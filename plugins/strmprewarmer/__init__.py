"""STRM 媒体信息预热插件。

STRM 入库后立即通过 Emby PlaybackInfo 接口触发真实媒体探测，
把分辨率、编码、码率、音轨等 MediaInfo 提前写入 Emby 媒体库，
减少首次播放前的 ffprobe 等待。功能移植自 emby-strm-prewarmer。
"""

import hashlib
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

# 宿主导入：优先使用 V3 稳定 SDK，找不到时回退 V2 旧路径，
# 使同一份实现可以在 MoviePilot V2 与 V3 宿主中运行。
# 只有「SDK 模块本身不存在」才回退；若是宿主第三方依赖缺失，
# 直接抛出原始错误，避免掩盖真实问题后在旧路径上报出误导性异常。
try:  # MoviePilot V3
    from app.sdk.events import Event, eventmanager
    from app.sdk.logging import logger
    from app.sdk.network import RequestUtils
    from app.sdk.services import MediaServerHelper
except ImportError as _sdk_error:  # MoviePilot V2
    _missing = getattr(_sdk_error, "name", "") or ""
    if not (_missing.startswith("app.sdk") or "app.sdk" in str(_sdk_error)):
        raise
    from app.core.event import Event, eventmanager
    from app.log import logger
    from app.utils.http import RequestUtils
    from app.helper.mediaserver import MediaServerHelper

from app.plugins import _PluginBase
from app.schemas import ServiceInfo
from app.schemas.types import EventType

try:  # V3 使用 MessageType
    from app.schemas.types import MessageType as _MsgType
except ImportError:  # V2 使用 NotificationType
    from app.schemas.types import NotificationType as _MsgType

# Emby 中需要探测媒体信息的条目类型
ITEM_TYPES = "Movie,Episode,Video"
# 可以执行 PlaybackInfo 探测的条目类型集合
PLAYABLE_TYPES = {"Movie", "Episode", "Video", "MusicVideo"}
# 查询条目时需要返回的字段
ITEM_FIELDS = "Path,MediaSources,MediaStreams"


class ApiResult(BaseModel):
    """插件 API 的统一返回结构，与宿主三段式响应保持一致。"""

    # 请求或业务操作是否成功
    success: bool
    # 给调用方展示的说明文本
    message: str = ""
    # 业务数据
    data: Dict[str, Any] = Field(default_factory=dict)


def is_playable(item: dict) -> bool:
    """判断条目是否是可以执行媒体探测的单个视频条目。

    Emby Webhook 在剧集入库时返回的是剧集（Series）ID，对这类条目执行
    PlaybackInfo 没有意义，必须按文件路径重新定位到具体分集。
    """
    if not item:
        return False
    item_type = item.get("Type")
    if item_type and item_type not in PLAYABLE_TYPES:
        return False
    return bool(item.get("Path"))


def normalize_base_url(host: str) -> str:
    """把用户配置的媒体服务器地址标准化为以 / 结尾的基础地址。"""
    host = (host or "").strip()
    if not host:
        return ""
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return host if host.endswith("/") else f"{host}/"


def split_mapping_line(line: str) -> Optional[Tuple[str, str]]:
    """解析一行路径映射配置，返回 (MoviePilot 路径, Emby 路径)。

    支持 ``=>``、``|``、``:`` 三种分隔符，其中 ``:`` 会跳过 Windows 盘符冒号。
    """
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    for separator in ("=>", "|"):
        if separator in line:
            left, right = line.split(separator, 1)
            break
    else:
        # 冒号分隔：从第 3 个字符开始查找，避免把 C:\ 的盘符冒号当成分隔符
        index = line.find(":", 2)
        if index < 0:
            return None
        left, right = line[:index], line[index + 1:]
    left, right = left.strip(), right.strip()
    if not left or not right:
        return None
    return left, right


def parse_mappings(text: str) -> List[Tuple[str, str]]:
    """解析多行路径映射配置，按前缀长度降序返回，保证最长前缀优先匹配。"""
    mappings = []
    for line in (text or "").splitlines():
        pair = split_mapping_line(line)
        if pair:
            mappings.append(pair)
    return sorted(mappings, key=lambda item: len(item[0]), reverse=True)


def parse_roots(text: str) -> Tuple[str, ...]:
    """解析媒体库根目录过滤配置，返回去空白后的目录元组。"""
    return tuple(
        line.strip() for line in (text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def apply_mapping(path: str, mappings: List[Tuple[str, str]], reverse: bool = False) -> str:
    """按映射表转换路径；reverse=True 时把 Emby 路径还原为本地路径。"""
    if not path:
        return path
    for left, right in mappings:
        source, target = (right, left) if reverse else (left, right)
        if not source:
            continue
        if path == source or path.startswith(source):
            remainder = path[len(source):]
            if target.endswith(("/", "\\")) and remainder.startswith(("/", "\\")):
                remainder = remainder[1:]
            elif target and not target.endswith(("/", "\\")) and remainder and not remainder.startswith(("/", "\\")):
                remainder = f"/{remainder}"
            return f"{target}{remainder}"
    return path


def is_strm(path: str) -> bool:
    """判断是否为 STRM 文件路径。"""
    return bool(path) and path.lower().endswith(".strm")


def media_streams(item: dict) -> List[dict]:
    """展开条目中所有媒体源的媒体流。"""
    streams = []
    for source in (item.get("MediaSources") or []):
        streams.extend(source.get("MediaStreams") or [])
    if not streams:
        streams = list(item.get("MediaStreams") or [])
    return streams


def has_complete_mediainfo(item: dict) -> bool:
    """判断条目是否已经具备完整的视频媒体信息（编码 + 分辨率）。"""
    if not item:
        return False
    return any(
        stream.get("Type") == "Video" and stream.get("Codec")
        and stream.get("Width") and stream.get("Height")
        for stream in media_streams(item)
    )


def describe_mediainfo(item: dict) -> str:
    """生成用于日志和历史展示的媒体信息摘要。"""
    if not item:
        return ""
    video, audio = None, []
    for stream in media_streams(item):
        if stream.get("Type") == "Video" and not video:
            video = stream
        elif stream.get("Type") == "Audio":
            audio.append(stream)
    parts = []
    if video:
        if video.get("Width") and video.get("Height"):
            parts.append(f"{video.get('Width')}x{video.get('Height')}")
        if video.get("Codec"):
            parts.append(str(video.get("Codec")).upper())
        bitrate = video.get("BitRate")
        if bitrate:
            try:
                parts.append(f"{int(bitrate) / 1000000:.1f}Mbps")
            except (TypeError, ValueError):
                pass
    if audio:
        codecs = sorted({str(stream.get("Codec")).upper() for stream in audio if stream.get("Codec")})
        if codecs:
            parts.append("/".join(codecs))
        parts.append(f"{len(audio)}音轨")
    return " ".join(parts)


def human_elapsed(seconds: float) -> str:
    """把秒数格式化为易读文本。"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}分{rest}秒"


def media_image(mediainfo: Any) -> str:
    """取媒体图片地址，优先使用消息图（横版背景图）。"""
    if not mediainfo:
        return ""
    for getter in ("get_message_image", "get_backdrop_image", "get_poster_image"):
        method = getattr(mediainfo, getter, None)
        if callable(method):
            try:
                image = method()
            except Exception:
                image = None
            if image:
                return str(image)
    for attribute in ("backdrop_path", "poster_path"):
        image = getattr(mediainfo, attribute, None)
        if image:
            return str(image)
    return ""


def describe_streams(item: dict) -> Dict[str, str]:
    """把媒体流拆成分辨率、编码、码率、音轨等字段，便于排版展示。"""
    video, audio, subtitle = None, [], []
    for stream in media_streams(item or {}):
        kind = stream.get("Type")
        if kind == "Video" and not video:
            video = stream
        elif kind == "Audio":
            audio.append(stream)
        elif kind == "Subtitle":
            subtitle.append(stream)
    fields: Dict[str, str] = {}
    if video:
        if video.get("Width") and video.get("Height"):
            fields["resolution"] = f"{video['Width']}x{video['Height']}"
        if video.get("Codec"):
            fields["codec"] = str(video["Codec"]).upper()
        if video.get("BitRate"):
            try:
                fields["bitrate"] = f"{int(video['BitRate']) / 1000000:.1f}Mbps"
            except (TypeError, ValueError):
                pass
        if video.get("VideoRange"):
            fields["range"] = str(video["VideoRange"])
    if audio:
        codecs = [str(stream.get("Codec")).upper() for stream in audio if stream.get("Codec")]
        fields["audio"] = f"{'/'.join(sorted(set(codecs)))}（{len(audio)}条）" if codecs else f"{len(audio)}条"
    if subtitle:
        fields["subtitle"] = f"{len(subtitle)}条"
    return fields


def file_fingerprint(local_path: Optional[str]) -> Dict[str, Any]:
    """计算 STRM 文件内容指纹，用于识别同名换源。"""
    if not local_path:
        return {"status": "unavailable"}
    try:
        candidate = Path(local_path)
        if not candidate.is_file():
            return {"status": "unavailable"}
        content = candidate.read_bytes()
        return {
            "status": "ok",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    except OSError:
        return {"status": "unavailable"}


class StrmPrewarmer(_PluginBase):
    """STRM 入库后立即预热 Emby 真实媒体信息的插件主类。"""

    # 插件名称
    plugin_name = "STRM媒体信息预热"
    # 插件描述
    plugin_desc = "STRM入库后立即调用Emby探测真实媒体信息（分辨率/编码/码率/音轨），避免首次播放等待。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Hotwill/MoviePilot-Plugins/main/icons/strmprewarmer.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Hotwill"
    # 作者主页
    author_url = "https://github.com/Hotwill"
    # 插件配置项ID前缀
    plugin_config_prefix = "strmprewarmer_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    def __init__(self):
        """初始化运行期属性，真实配置在 init_plugin 中读取。"""
        super().__init__()
        self._enabled = False
        self._notify = False
        self._notify_success = False
        self._mediaservers: List[str] = []
        self._only_strm = True
        self._listen_transfer = True
        self._listen_webhook = True
        self._delay = 10
        self._wait_timeout = 600
        self._poll_interval = 15
        self._max_retries = 2
        self._retry_interval = 10
        self._timeout = 300
        self._max_bitrate = 200000000
        self._refresh_missing = False
        self._deep_lookup = True
        self._deep_lookup_limit = 20000
        self._dedup_window = 600
        self._cron = ""
        self._onlyonce = False
        self._scan_roots: Tuple[str, ...] = ()
        self._mappings: List[Tuple[str, str]] = []
        self._max_items = 0
        self._item_interval = 1.0
        self._history_count = 200
        # 运行期资源
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._inflight: set = set()
        # 最近成功处理过的条目，避免入库事件与 Webhook 重复预热
        self._recent: Dict[Tuple[str, str], float] = {}
        self._scanning = False

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建后台工作线程，可重复调用。"""
        # 先停掉上一轮的后台线程，避免重载后重复消费队列
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._notify_success = bool(config.get("notify_success"))
        self._mediaservers = config.get("mediaservers") or []
        self._only_strm = bool(config.get("only_strm", True))
        self._listen_transfer = bool(config.get("listen_transfer", True))
        self._listen_webhook = bool(config.get("listen_webhook", True))
        self._delay = self._to_int(config.get("delay"), 10)
        self._wait_timeout = self._to_int(config.get("wait_timeout"), 600)
        self._poll_interval = max(3, self._to_int(config.get("poll_interval"), 15))
        self._max_retries = self._to_int(config.get("max_retries"), 2)
        self._retry_interval = self._to_int(config.get("retry_interval"), 10)
        self._timeout = max(30, self._to_int(config.get("timeout"), 300))
        self._max_bitrate = self._to_int(config.get("max_bitrate"), 200000000)
        self._refresh_missing = bool(config.get("refresh_missing"))
        self._deep_lookup = bool(config.get("deep_lookup", True))
        self._deep_lookup_limit = max(500, self._to_int(config.get("deep_lookup_limit"), 20000))
        self._dedup_window = max(0, self._to_int(config.get("dedup_window"), 600))
        self._cron = (config.get("cron") or "").strip()
        self._onlyonce = bool(config.get("onlyonce"))
        self._scan_roots = parse_roots(config.get("scan_roots"))
        self._mappings = parse_mappings(config.get("path_mappings"))
        self._max_items = self._to_int(config.get("max_items"), 0)
        self._item_interval = max(0.0, self._to_float(config.get("item_interval"), 1.0))
        self._history_count = max(20, self._to_int(config.get("history_count"), 200))

        # 重置停止标记与任务队列，保证重载后不会残留上一轮状态
        self._stop_event = threading.Event()
        self._queue = queue.Queue()

        if not self._enabled:
            return

        worker = threading.Thread(target=self._worker_loop, name="StrmPrewarmerWorker", daemon=True)
        worker.start()
        self._workers = [worker]
        logger.info("STRM媒体信息预热插件已启动")

        if self._onlyonce:
            # 立即执行一次全量补漏扫描，并复位一次性开关
            self._onlyonce = False
            self.update_config(self._current_config())
            self._queue.put({"type": "scan", "source": "手动"})

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        """把配置值安全转换为整数。"""
        try:
            if value is None or value == "":
                return default
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        """把配置值安全转换为浮点数。"""
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _current_config(self) -> dict:
        """汇总当前配置，用于回写插件配置。"""
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "notify_success": self._notify_success,
            "mediaservers": self._mediaservers,
            "only_strm": self._only_strm,
            "listen_transfer": self._listen_transfer,
            "listen_webhook": self._listen_webhook,
            "delay": self._delay,
            "wait_timeout": self._wait_timeout,
            "poll_interval": self._poll_interval,
            "max_retries": self._max_retries,
            "retry_interval": self._retry_interval,
            "timeout": self._timeout,
            "max_bitrate": self._max_bitrate,
            "refresh_missing": self._refresh_missing,
            "deep_lookup": self._deep_lookup,
            "deep_lookup_limit": self._deep_lookup_limit,
            "dedup_window": self._dedup_window,
            "cron": self._cron,
            "onlyonce": False,
            "scan_roots": "\n".join(self._scan_roots),
            "path_mappings": "\n".join(f"{left} => {right}" for left, right in self._mappings),
            "max_items": self._max_items,
            "item_interval": self._item_interval,
            "history_count": self._history_count,
        }

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return self._enabled

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """返回已连接的 Emby 服务实例，未配置或全部离线时返回 None。"""
        helper = MediaServerHelper()
        try:
            services = helper.get_services(type_filter="emby", name_filters=self._mediaservers or None)
        except TypeError:
            # 兼容不支持 type_filter 的宿主版本
            services = helper.get_services(name_filters=self._mediaservers or None)
        if not services:
            logger.warning("未获取到 Emby 媒体服务器实例，请检查插件配置")
            return None
        active = {}
        for name, service in services.items():
            if getattr(service, "type", None) not in (None, "emby"):
                continue
            try:
                if service.instance.is_inactive():
                    logger.warning(f"媒体服务器 {name} 未连接，跳过")
                    continue
            except Exception as err:
                logger.warning(f"媒体服务器 {name} 状态检查失败：{err}")
                continue
            active[name] = service
        if not active:
            logger.warning("没有已连接的 Emby 媒体服务器")
            return None
        return active

    @staticmethod
    def _endpoint(service: ServiceInfo) -> Optional[Tuple[str, str, str]]:
        """从服务配置中取出 Emby 的 host、apikey 和用户 ID。"""
        config = getattr(getattr(service, "config", None), "config", None) or {}
        host = normalize_base_url(config.get("host") or getattr(service.instance, "_host", "") or "")
        apikey = config.get("apikey") or getattr(service.instance, "_apikey", "") or ""
        user = getattr(service.instance, "user", "") or ""
        if not host or not apikey:
            return None
        return host, apikey, str(user)

    def _request(self, service: ServiceInfo, method: str, path: str,
                 params: dict = None, json_body: Any = None) -> Tuple[bool, Any]:
        """向 Emby 发起请求，返回 (是否成功, 解析后的 JSON 或错误信息)。"""
        endpoint = self._endpoint(service)
        if not endpoint:
            return False, "媒体服务器地址或密钥缺失"
        host, apikey, user = endpoint
        url = f"{host}emby/{path.lstrip('/')}"
        query = dict(params or {})
        query["api_key"] = apikey
        if "UserId" in query and query["UserId"] == "[USER]":
            query["UserId"] = user
        request = RequestUtils(timeout=self._timeout, content_type="application/json")
        try:
            if method.upper() == "POST":
                response = request.post_res(url, params=query, json=json_body if json_body is not None else {})
            else:
                response = request.get_res(url, params=query)
        except Exception as err:
            return False, f"{type(err).__name__}: {err}"
        if response is None:
            return False, "请求媒体服务器无响应"
        if response.status_code not in (200, 204):
            return False, f"HTTP {response.status_code}"
        if not response.content:
            return True, None
        try:
            return True, response.json()
        except ValueError:
            return True, None

    def _user_id(self, service: ServiceInfo) -> Optional[str]:
        """获取用于播放信息请求的用户 ID。"""
        endpoint = self._endpoint(service)
        if endpoint and endpoint[2]:
            return endpoint[2]
        ok, data = self._request(service, "GET", "Users")
        if ok and isinstance(data, list) and data:
            return str(data[0].get("Id") or "")
        return None

    def _fetch_item(self, service: ServiceInfo, item_id: str) -> Optional[dict]:
        """获取单个条目的详情（含媒体流）。"""
        user = self._user_id(service)
        path = f"Users/{user}/Items/{item_id}" if user else f"Items/{item_id}"
        ok, data = self._request(service, "GET", path, {"Fields": ITEM_FIELDS})
        if ok and isinstance(data, dict) and data.get("Id"):
            return data
        if not ok:
            logger.debug(f"获取条目 {item_id} 详情失败：{data}")
        return None

    def _query_items(self, service: ServiceInfo, params: dict) -> List[dict]:
        """按查询条件获取条目列表。"""
        query = {
            "Recursive": "true",
            "IncludeItemTypes": ITEM_TYPES,
            "Fields": ITEM_FIELDS,
        }
        query.update(params)
        ok, data = self._request(service, "GET", "Items", query)
        if not ok or not isinstance(data, dict):
            return []
        return data.get("Items") or []

    def _find_item_by_path(self, service: ServiceInfo, emby_path: str) -> Optional[dict]:
        """根据 Emby 内部路径查找条目：Path 过滤 -> 文件名搜索 -> 深度遍历。"""
        if not emby_path:
            return None
        target = emby_path.replace("\\", "/").lower()
        # 查询策略按代价从低到高惰性执行，命中即返回，避免多余请求
        strategies = (
            lambda: self._query_items(service, {"Path": emby_path, "Limit": "20"}),
            lambda: self._query_items(service, {"SearchTerm": Path(emby_path).stem, "Limit": "50"}),
        )
        for strategy in strategies:
            items = strategy()
            for item in items:
                item_path = (item.get("Path") or "").replace("\\", "/").lower()
                if item_path == target:
                    return item
            # 精确过滤只返回一条且文件名一致时直接采用
            if len(items) == 1 and items[0].get("Path"):
                only_path = items[0]["Path"].replace("\\", "/").lower()
                if Path(only_path).name == Path(target).name:
                    return items[0]
        if self._deep_lookup:
            return self._find_item_by_scan(service, target)
        return None

    def _find_item_by_scan(self, service: ServiceInfo, target: str) -> Optional[dict]:
        """分页遍历媒体库按路径匹配条目，兜底老版本 Emby 不支持 Path 过滤的情况。"""
        start, page, scanned = 0, 500, 0
        while scanned < self._deep_lookup_limit and not self._stop_event.is_set():
            items = self._query_items(service, {
                "StartIndex": str(start), "Limit": str(page), "Fields": "Path"})
            if not items:
                return None
            for item in items:
                if (item.get("Path") or "").replace("\\", "/").lower() == target:
                    # 遍历时只取了 Path 字段，需要回查完整详情
                    return self._fetch_item(service, str(item.get("Id"))) or item
            scanned += len(items)
            if len(items) < page:
                return None
            start += page
        return None

    def _trigger_refresh(self, service: ServiceInfo, item_id: str = None) -> None:
        """触发 Emby 刷新：指定条目时刷新单项，否则刷新媒体库根。"""
        if item_id:
            ok, message = self._request(service, "POST", f"Items/{item_id}/Refresh", {
                "Recursive": "false",
                "MetadataRefreshMode": "FullRefresh",
                "ImageRefreshMode": "None",
                "ReplaceAllMetadata": "false",
            })
            if not ok:
                logger.warning(f"刷新条目 {item_id} 失败：{message}")
            return
        ok, message = self._request(service, "POST", "Library/Refresh")
        if ok:
            logger.info("已请求 Emby 扫描媒体库")
        else:
            logger.warning(f"请求 Emby 扫描媒体库失败：{message}")

    def _prewarm(self, service: ServiceInfo, item_id: str) -> Tuple[bool, str]:
        """调用 PlaybackInfo 触发真实媒体探测，并校验结果是否完整。"""
        user = self._user_id(service)
        params = {
            "IsPlayback": "true",
            "AutoOpenLiveStream": "true",
            "MaxStreamingBitrate": str(self._max_bitrate),
        }
        if user:
            params["UserId"] = user
        ok, message = self._request(service, "POST", f"Items/{item_id}/PlaybackInfo", params, json_body={})
        if not ok:
            return False, str(message)
        item = self._fetch_item(service, item_id)
        if not has_complete_mediainfo(item):
            return False, "PlaybackInfo 已完成但媒体信息仍不完整"
        return True, describe_mediainfo(item)

    def _local_path(self, emby_path: str) -> str:
        """把 Emby 路径还原为 MoviePilot 可访问的本地路径。"""
        return apply_mapping(emby_path, self._mappings, reverse=True)

    def _emby_path(self, local_path: str) -> str:
        """把 MoviePilot 本地路径转换为 Emby 内部路径。"""
        return apply_mapping(local_path, self._mappings)

    def _fingerprints(self) -> dict:
        """读取已保存的 STRM 指纹表。"""
        return self.get_data("fingerprints") or {}

    def _save_fingerprint(self, key: str, path: str, signature: dict) -> None:
        """保存单个条目的 STRM 指纹。"""
        if signature.get("status") != "ok":
            return
        data = self._fingerprints()
        data[str(key)] = {"path": path, "signature": signature}
        self.save_data("fingerprints", data)

    def _wait_for_item(self, service: ServiceInfo, emby_path: str, title: str) -> Optional[dict]:
        """轮询等待 Emby 识别到指定路径的条目。"""
        deadline = time.time() + max(self._poll_interval, self._wait_timeout)
        refreshed = False
        while not self._stop_event.is_set():
            item = self._find_item_by_path(service, emby_path)
            if item:
                return item
            if self._refresh_missing and not refreshed:
                refreshed = True
                self._trigger_refresh(service)
            if time.time() >= deadline:
                return None
            logger.debug(f"等待 Emby 识别 {title or emby_path} ...")
            if self._stop_event.wait(self._poll_interval):
                return None
        return None

    def _process_target(self, service_name: str, service: ServiceInfo, item: dict,
                        title: str, source: str, reason: str = None,
                        image: str = "") -> dict:
        """对单个条目执行预热，返回历史记录。

        reason 为 None 时自行判断是否需要预热；``incomplete`` 表示调用方已确认
        媒体信息缺失；``changed`` 表示 STRM 换源，需要先刷新条目再预热。
        """
        item_id = str(item.get("Id"))
        item_path = item.get("Path") or ""
        display = title or item.get("Name") or item_path
        local_path = self._local_path(item_path)
        signature = file_fingerprint(local_path)
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "server": service_name,
            "item_id": item_id,
            "title": display,
            "path": item_path,
            "source": source,
            "status": "skip",
            "detail": "",
            "image": image or "",
            "filename": Path(item_path).name if item_path else "",
        }

        if reason == "changed":
            # 调用方已确认换源，先刷新条目让 Emby 丢弃旧的媒体信息
            logger.info(f"{display} STRM 源已变更，重新刷新并预热")
            record["changed"] = True
            self._trigger_refresh(service, item_id)
            self._stop_event.wait(2)
        elif reason is None and has_complete_mediainfo(item):
            fingerprints = self._fingerprints()
            previous = (fingerprints.get(item_id) or {}).get("signature")
            if signature.get("status") != "ok" or previous is None or previous == signature:
                # 已有完整媒体信息且源文件未变化，记录基线后跳过
                self._save_fingerprint(item_id, item_path, signature)
                record["detail"] = f"已有媒体信息 {describe_mediainfo(item)}".strip()
                logger.info(f"{display} 已有完整媒体信息，跳过预热")
                return record
            logger.info(f"{display} STRM 源已变更，重新刷新并预热")
            record["changed"] = True
            self._trigger_refresh(service, item_id)
            self._stop_event.wait(2)

        started = time.time()
        error = ""
        for attempt in range(self._max_retries + 1):
            if self._stop_event.is_set():
                record["status"] = "fail"
                record["detail"] = "插件已停止"
                return record
            ok, message = self._prewarm(service, item_id)
            if ok:
                elapsed = time.time() - started
                record["status"] = "changed" if record.get("changed") else "success"
                prefix = "换源重新预热 " if record.get("changed") else ""
                record["detail"] = f"{prefix}{message} 用时{human_elapsed(elapsed)}".strip()
                record["elapsed"] = round(elapsed, 1)
                record["media"] = describe_streams(self._fetch_item(service, item_id))
                self._save_fingerprint(item_id, item_path, signature)
                logger.info(f"预热成功 {display} {message} 用时{elapsed:.1f}s")
                return record
            error = str(message)
            if attempt < self._max_retries:
                logger.warning(f"预热失败 {display}：{error}，{self._retry_interval}s 后重试")
                if self._stop_event.wait(self._retry_interval):
                    break
        record["status"] = "fail"
        record["detail"] = error
        logger.error(f"预热失败 {display}：{error}")
        return record

    def _worker_loop(self) -> None:
        """后台工作线程：串行消费预热任务，避免阻塞入库流程。"""
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                if task.get("type") == "scan":
                    self.full_scan(source=task.get("source") or "定时")
                else:
                    self._handle_task(task)
            except Exception as err:
                logger.error(f"处理预热任务出错：{err}")
            finally:
                self._queue.task_done()

    def _locate_item(self, service: ServiceInfo, task: dict, title: str) -> Optional[dict]:
        """定位任务对应的 Emby 条目。

        优先按文件路径定位：Emby Webhook 在剧集入库时给出的 item_id 是剧集 ID，
        只有路径才能唯一定位到具体分集。条目 ID 仅作为路径不可用时的回退。
        """
        raw_path = task.get("path")
        if raw_path:
            # 入库事件给出的是 MoviePilot 侧路径，需要映射；Webhook 给出的已是 Emby 侧路径
            from_emby = task.get("path_side") == "emby"
            emby_path = raw_path if from_emby else self._emby_path(raw_path)
            # Webhook 触发时条目必然已在库中，直接查一次即可，不必轮询等待
            item = (self._find_item_by_path(service, emby_path) if from_emby
                    else self._wait_for_item(service, emby_path, title))
            if item:
                return item
        if task.get("item_id"):
            candidate = self._fetch_item(service, str(task["item_id"]))
            if is_playable(candidate):
                return candidate
            if candidate:
                logger.debug(f"条目 {task['item_id']} 类型为 {candidate.get('Type')}，不能直接探测")
        return None

    def _handle_task(self, task: dict) -> None:
        """处理单个入库预热任务：定位条目 -> 预热 -> 记录。"""
        delay = self._delay if task.get("delay") is None else self._to_int(task.get("delay"), self._delay)
        if delay > 0 and self._stop_event.wait(delay):
            return
        services = self.service_infos
        if not services:
            return
        title = task.get("title") or ""
        records = []
        for name, service in services.items():
            if task.get("server") and task.get("server") != name:
                continue
            item = self._locate_item(service, task, title)
            if not item:
                logger.warning(f"{title or task.get('path') or task.get('item_id')} "
                               f"在 {name} 中未找到可探测的条目，已跳过预热")
                continue
            if self._only_strm and not is_strm(item.get("Path") or ""):
                logger.debug(f"{item.get('Path')} 不是 STRM 文件，按配置跳过")
                continue
            key = (name, str(item.get("Id")))
            with self._lock:
                if key in self._inflight or self._recently_done(key):
                    logger.debug(f"{title or item.get('Path')} 正在处理或刚处理过，跳过重复触发")
                    continue
                self._inflight.add(key)
            try:
                records.append(self._process_target(name, service, item, title,
                                                    task.get("source") or "入库",
                                                    image=task.get("image") or ""))
            finally:
                with self._lock:
                    self._inflight.discard(key)
                    self._mark_done(key)
        if records:
            self._record_history(records)
            self._notify_records(records)

    def _recently_done(self, key: Tuple[str, str]) -> bool:
        """判断条目是否在去重窗口内已经处理过；调用方需持有锁。"""
        if not self._dedup_window:
            return False
        done_at = self._recent.get(key)
        return bool(done_at and time.time() - done_at < self._dedup_window)

    def _mark_done(self, key: Tuple[str, str]) -> None:
        """记录条目处理时间并清理过期记录；调用方需持有锁。"""
        now = time.time()
        self._recent[key] = now
        if len(self._recent) > 500:
            window = self._dedup_window or 600
            self._recent = {item: at for item, at in self._recent.items() if now - at < window}

    def _enqueue_paths(self, paths: List[str], title: str, source: str, server: str = None,
                       image: str = "") -> None:
        """把待预热的文件路径放入任务队列。"""
        for path in paths:
            if not path:
                continue
            if self._only_strm and not is_strm(path):
                continue
            self._queue.put({
                "type": "path",
                "path": path,
                "path_side": "local",
                "title": title,
                "source": source,
                "server": server,
                "image": image,
            })
            logger.info(f"已加入预热队列：{title or path}")

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """监听整理入库完成事件，对新入库的 STRM 触发预热。"""
        if not self._enabled or not self._listen_transfer:
            return
        event_data = event.event_data or {}
        transferinfo = event_data.get("transferinfo")
        mediainfo = event_data.get("mediainfo")
        if not transferinfo:
            return
        title = getattr(mediainfo, "title_year", None) or getattr(mediainfo, "title", None) or ""
        paths = []
        target_item = getattr(transferinfo, "target_item", None)
        if target_item is not None and getattr(target_item, "path", None):
            paths.append(str(target_item.path))
        for path in (getattr(transferinfo, "file_list_new", None) or []):
            if str(path) not in paths:
                paths.append(str(path))
        if not paths:
            return
        self._enqueue_paths(paths, title, "入库", image=media_image(mediainfo))

    @eventmanager.register(EventType.WebhookMessage)
    def on_webhook_message(self, event: Event) -> None:
        """监听媒体服务器入库 Webhook，对新增条目立即预热。"""
        if not self._enabled or not self._listen_webhook:
            return
        event_info = event.event_data
        if not event_info:
            return
        channel = str(getattr(event_info, "channel", "") or "").lower()
        if channel and channel != "emby":
            return
        event_name = str(getattr(event_info, "event", "") or "").lower()
        if event_name not in ("library.new", "item.added", "library.add"):
            return
        item_path = getattr(event_info, "item_path", None)
        if self._only_strm and item_path and not is_strm(str(item_path)):
            return
        item_id = getattr(event_info, "item_id", None)
        if not item_id and not item_path:
            return
        self._queue.put({
            "type": "item",
            "item_id": str(item_id) if item_id else None,
            "path": str(item_path) if item_path else None,
            "path_side": "emby",
            "title": getattr(event_info, "item_name", "") or "",
            "source": "Webhook",
            "server": getattr(event_info, "server_name", None),
            "delay": 0,
            "image": str(getattr(event_info, "image_url", "") or ""),
        })
        logger.info(f"Webhook 新增入库，已加入预热队列：{getattr(event_info, 'item_name', '') or item_id}")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """响应远程命令，手动触发一次全量补漏扫描。"""
        if not self._enabled:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != "strm_prewarm":
            return
        self._queue.put({"type": "scan", "source": "命令"})

    def full_scan(self, source: str = "定时") -> None:
        """全量扫描媒体库，补齐缺失媒体信息并识别同名换源。"""
        if self._scanning:
            logger.info("已有扫描任务在执行，跳过本次全量扫描")
            return
        services = self.service_infos
        if not services:
            return
        self._scanning = True
        records = []
        try:
            for name, service in services.items():
                targets = self._collect_scan_targets(service, name)
                logger.info(f"{name} 待预热 STRM 条目 {len(targets)} 个")
                for index, (item, reason) in enumerate(targets, 1):
                    if self._stop_event.is_set():
                        break
                    key = (name, str(item.get("Id")))
                    with self._lock:
                        if key in self._inflight:
                            continue
                        self._inflight.add(key)
                    try:
                        records.append(self._process_target(
                            name, service, item, item.get("Name") or "", source, reason=reason))
                    finally:
                        with self._lock:
                            self._inflight.discard(key)
                    if index < len(targets) and self._item_interval:
                        if self._stop_event.wait(self._item_interval):
                            break
        finally:
            self._scanning = False
        if records:
            self._record_history(records)
            handled = [record for record in records if record.get("status") != "skip"]
            if handled:
                self._notify_records(handled, summary=True)

    def _collect_scan_targets(self, service: ServiceInfo, service_name: str) -> List[Tuple[dict, str]]:
        """分页扫描媒体库，返回需要预热的条目及原因（incomplete / changed）。"""
        targets: List[Tuple[dict, str]] = []
        fingerprints = self._fingerprints()
        baseline = {}
        start, page = 0, 500
        while not self._stop_event.is_set():
            items = self._query_items(service, {"StartIndex": str(start), "Limit": str(page)})
            if not items:
                break
            for item in items:
                item_path = item.get("Path") or ""
                if self._only_strm and not is_strm(item_path):
                    continue
                if self._scan_roots and not item_path.startswith(self._scan_roots):
                    continue
                item_id = str(item.get("Id"))
                if not has_complete_mediainfo(item):
                    targets.append((item, "incomplete"))
                    continue
                signature = file_fingerprint(self._local_path(item_path))
                if signature.get("status") != "ok":
                    continue
                previous = (fingerprints.get(item_id) or {}).get("signature")
                if previous is None:
                    baseline[item_id] = {"path": item_path, "signature": signature}
                elif previous != signature:
                    targets.append((item, "changed"))
                if self._max_items and len(targets) >= self._max_items:
                    break
            if self._max_items and len(targets) >= self._max_items:
                break
            if len(items) < page:
                break
            start += page
        if baseline:
            # 首次扫描到的完整条目记录指纹基线，后续才能识别换源
            fingerprints.update(baseline)
            self.save_data("fingerprints", fingerprints)
            logger.info(f"{service_name} 记录 {len(baseline)} 个 STRM 指纹基线")
        if self._max_items:
            targets = targets[:self._max_items]
        return targets

    def _record_history(self, records: List[dict]) -> None:
        """写入运行历史，只保留最近若干条。"""
        history = self.get_data("history") or []
        history = list(records) + list(history)
        self.save_data("history", history[:self._history_count])

    @staticmethod
    def _detail_text(record: dict) -> str:
        """拼装单条记录的通知正文，风格贴近 MoviePilot 的入库通知。"""
        media = record.get("media") or {}
        lines = []
        if media.get("resolution"):
            quality = media["resolution"]
            if media.get("range") and media["range"].upper() not in ("SDR", ""):
                quality += f" {media['range']}"
            lines.append(f"🖼️ 画面：{quality}")
        if media.get("codec") or media.get("bitrate"):
            codec = " · ".join(part for part in (media.get("codec"), media.get("bitrate")) if part)
            lines.append(f"🎞️ 编码：{codec}")
        if media.get("audio"):
            lines.append(f"🔊 音轨：{media['audio']}")
        if media.get("subtitle"):
            lines.append(f"💬 字幕：{media['subtitle']}")
        if record.get("elapsed"):
            lines.append(f"⏱️ 耗时：{human_elapsed(float(record['elapsed']))}")
        if record.get("server"):
            lines.append(f"📺 服务器：{record['server']}")
        if record.get("status") == "fail" and record.get("detail"):
            lines.append(f"⚠️ 原因：{record['detail']}")
        if record.get("status") == "changed":
            lines.append("🔄 检测到 STRM 换源，已重新探测")
        if record.get("filename"):
            lines.append(f"📄 文件：{record['filename']}")
        if not lines and record.get("detail"):
            lines.append(record["detail"])
        return "\n".join(lines)

    def _notify_records(self, records: List[dict], summary: bool = False) -> None:
        """按配置推送预热结果通知，带媒体图片与分行排版。"""
        if not self._notify or not records:
            return
        success = [record for record in records if record.get("status") in ("success", "changed")]
        failed = [record for record in records if record.get("status") == "fail"]
        if not failed and not (success and self._notify_success):
            return
        shown = success + failed
        image = next((record.get("image") for record in shown if record.get("image")), "")
        if len(shown) == 1:
            record = shown[0]
            flag = "❌ 媒体信息预热失败" if record.get("status") == "fail" else "✅ 媒体信息已预热"
            self.post_message(
                mtype=_MsgType.Plugin,
                title=f"{record.get('title') or 'STRM 媒体'} {flag}",
                text=self._detail_text(record),
                image=image or None,
            )
            return
        header = [f"✅ 成功 {len(success)} 个"] if success else []
        if failed:
            header.append(f"❌ 失败 {len(failed)} 个")
        lines = [" · ".join(header)] if header else []
        for record in shown[:15]:
            flag = {"success": "✅", "changed": "🔄", "fail": "❌"}.get(record.get("status"), "➖")
            media = record.get("media") or {}
            brief = " ".join(part for part in (media.get("resolution"), media.get("codec")) if part) \
                or (record.get("detail") or "")
            lines.append(f"{flag} {record.get('title')}" + (f"（{brief}）" if brief else ""))
        if len(shown) > 15:
            lines.append(f"…… 其余 {len(shown) - 15} 个见插件详情页")
        title = "STRM媒体信息预热完成" if not failed else (
            "STRM媒体信息预热失败" if not success else f"STRM媒体信息预热完成，失败 {len(failed)} 个")
        self.post_message(mtype=_MsgType.Plugin, title=title,
                          text="\n".join(lines), image=image or None)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册手动触发全量补漏扫描的远程命令。"""
        return [
            {
                "cmd": "/strm_prewarm",
                "event": EventType.PluginAction,
                "desc": "STRM媒体信息预热",
                "category": "插件命令",
                "data": {"action": "strm_prewarm"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：查询状态、查询历史、外部触发预热。"""
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询预热插件状态",
                "response_model": ApiResult,
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询预热历史",
                "response_model": ApiResult,
            },
            {
                "path": "/prewarm",
                "endpoint": self.api_prewarm,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "外部触发 STRM 预热",
                "response_model": ApiResult,
            },
        ]

    def api_status(self) -> "ApiResult":
        """返回插件运行状态，供页面或外部系统查询。"""
        history = self.get_data("history") or []
        counts: Dict[str, int] = {}
        for record in history:
            status = str(record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return ApiResult(success=True, data={
            "enabled": self.get_state(),
            "queued": self._queue.qsize(),
            "processing": len(self._inflight),
            "scanning": self._scanning,
            "listen_transfer": self._listen_transfer,
            "listen_webhook": self._listen_webhook,
            "only_strm": self._only_strm,
            "cron": self._cron,
            "history_counts": counts,
            "last_record": history[0] if history else None,
        })

    def api_history(self, limit: int = 50) -> "ApiResult":
        """返回最近的预热历史记录。"""
        history = self.get_data("history") or []
        limit = max(1, min(int(limit or 50), self._history_count))
        return ApiResult(success=True, data={"total": len(history), "records": history[:limit]})

    def api_prewarm(self, path: str = None, item_id: str = None, server: str = None,
                    side: str = "local", delay: int = None) -> "ApiResult":
        """外部触发预热，适用于由第三方工具生成 STRM 的场景。

        :param path: STRM 文件路径
        :param item_id: Emby 条目 ID，与 path 二选一
        :param server: 限定媒体服务器名称，留空表示全部
        :param side: path 属于哪一侧，``local`` 为 MoviePilot 路径，``emby`` 为 Emby 路径
        :param delay: 覆盖入库后延迟秒数
        """
        if not self._enabled:
            return ApiResult(success=False, message="插件未启用")
        if not path and not item_id:
            return ApiResult(success=False, message="缺少参数：path 或 item_id")
        if path and self._only_strm and not is_strm(path):
            return ApiResult(success=False, message="仅处理 STRM 文件，可在插件配置中关闭该限制")
        if side not in ("local", "emby"):
            return ApiResult(success=False, message="side 只能是 local 或 emby")
        self._queue.put({
            "type": "item" if item_id else "path",
            "path": path,
            "path_side": side,
            "item_id": item_id,
            "title": Path(path).stem if path else (item_id or ""),
            "source": "API",
            "server": server,
            "delay": delay,
        })
        logger.info(f"API 触发预热：{path or item_id}")
        return ApiResult(success=True, message="已加入预热队列", data={
            "queued": self._queue.qsize(),
            "path": path,
            "item_id": item_id,
        })


    def get_service(self) -> List[Dict[str, Any]]:
        """按配置注册定时全量补漏扫描任务。"""
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(f"定时扫描周期配置错误：{err}")
            return []
        return [
            {
                "id": "StrmPrewarmer.FullScan",
                "name": "STRM媒体信息预热全量扫描",
                "trigger": trigger,
                "func": self.full_scan,
                "kwargs": {},
            }
        ]

    def stop_service(self) -> None:
        """停止后台线程并释放资源，可重复调用。"""
        self._stop_event.set()
        for worker in self._workers:
            if worker.is_alive():
                worker.join(timeout=5)
        self._workers = []
        with self._lock:
            self._inflight.clear()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面与默认配置模型。"""
        try:
            server_items = [{"title": conf.name, "value": conf.name}
                            for conf in MediaServerHelper().get_configs().values()
                            if getattr(conf, "type", None) == "emby"]
        except Exception:
            server_items = []

        def switch(model: str, label: str, hint: str = "", cols: int = 4) -> dict:
            """生成一个开关配置项。"""
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": cols},
                "content": [{
                    "component": "VSwitch",
                    "props": {"model": model, "label": label, "hint": hint, "persistent-hint": bool(hint)},
                }],
            }

        def text(model: str, label: str, placeholder: str = "", hint: str = "", cols: int = 4) -> dict:
            """生成一个文本输入配置项。"""
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": cols},
                "content": [{
                    "component": "VTextField",
                    "props": {"model": model, "label": label, "placeholder": placeholder,
                              "hint": hint, "persistent-hint": bool(hint)},
                }],
            }

        def row(content: List[dict]) -> dict:
            """生成一行配置项。"""
            return {"component": "VRow", "content": content}

        return [{
            "component": "VForm",
            "content": [
                row([
                    switch("enabled", "启用插件"),
                    switch("notify", "发送通知"),
                    switch("notify_success", "成功也通知"),
                ]),
                row([{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VSelect",
                        "props": {
                            "multiple": True, "chips": True, "clearable": True,
                            "model": "mediaservers", "label": "Emby 服务器",
                            "items": server_items,
                            "hint": "留空表示使用全部已启用的 Emby 服务器",
                            "persistent-hint": True,
                        },
                    }],
                }]),
                row([
                    switch("listen_transfer", "监听整理入库", "MoviePilot 整理完成后自动预热"),
                    switch("listen_webhook", "监听媒体库Webhook", "Emby library.new 事件触发预热"),
                    switch("only_strm", "仅处理STRM", "关闭后所有新入库媒体都会预热"),
                ]),
                row([
                    text("delay", "入库后延迟（秒）", "10", "等待 Emby 完成扫描的缓冲时间"),
                    text("wait_timeout", "等待识别超时（秒）", "600", "超时仍未找到条目则放弃"),
                    text("poll_interval", "查找间隔（秒）", "15"),
                ]),
                row([
                    text("max_retries", "失败重试次数", "2"),
                    text("retry_interval", "重试间隔（秒）", "10"),
                    text("timeout", "请求超时（秒）", "300", "网盘响应慢时可增大"),
                ]),
                row([
                    text("cron", "定时补漏扫描", "0 3 * * *", "留空则不执行定时全量扫描"),
                    text("max_items", "单次最多处理", "0", "0 表示不限制"),
                    text("item_interval", "条目间隔（秒）", "1"),
                ]),
                row([
                    switch("refresh_missing", "找不到时扫描媒体库", "未找到条目时请求 Emby 扫描"),
                    switch("onlyonce", "立即运行一次", "保存后立即执行一次全量扫描"),
                    switch("deep_lookup", "深度查找条目", "Path 查询不可用时遍历媒体库匹配路径"),
                ]),
                row([
                    text("max_bitrate", "探测码率上限", "200000000", "PlaybackInfo 请求参数"),
                    text("deep_lookup_limit", "深度查找上限", "20000", "深度查找最多遍历的条目数"),
                    text("dedup_window", "去重窗口（秒）", "600", "窗口内同一条目不重复预热"),
                ]),
                row([{
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [{
                        "component": "VTextarea",
                        "props": {
                            "model": "path_mappings", "label": "路径映射（MoviePilot => Emby）",
                            "rows": 4,
                            "placeholder": "/media/strm => /data/media/strm",
                            "hint": "每行一条，支持 => 、| 或 : 分隔；同时用于反查 STRM 本地文件",
                            "persistent-hint": True,
                        },
                    }],
                }, {
                    "component": "VCol",
                    "props": {"cols": 12, "md": 6},
                    "content": [{
                        "component": "VTextarea",
                        "props": {
                            "model": "scan_roots", "label": "全量扫描目录（Emby 路径）",
                            "rows": 4,
                            "placeholder": "/data/media/movies\n/data/media/tv",
                            "hint": "留空表示扫描全部媒体库，仅影响定时/手动全量扫描",
                            "persistent-hint": True,
                        },
                    }],
                }]),
                row([{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VAlert",
                        "props": {
                            "type": "info", "variant": "tonal",
                            "text": "工作原理：STRM 入库后调用 Emby PlaybackInfo 接口，"
                                    "让 Emby 用 ffprobe 读取真实媒体信息并保存到自己的媒体库。"
                                    "不下载完整视频、不修改 STRM 和 NFO、不写入虚假信息。"
                                    "若 Emby 尚未扫描到新文件，可搭配官方「媒体库服务器刷新」插件或开启实时监控。",
                        },
                    }],
                }]),
            ],
        }], {
            "enabled": False,
            "notify": False,
            "notify_success": False,
            "mediaservers": [],
            "listen_transfer": True,
            "listen_webhook": True,
            "only_strm": True,
            "delay": 10,
            "wait_timeout": 600,
            "poll_interval": 15,
            "max_retries": 2,
            "retry_interval": 10,
            "timeout": 300,
            "cron": "",
            "max_items": 0,
            "item_interval": 1,
            "refresh_missing": False,
            "onlyonce": False,
            "deep_lookup": True,
            "deep_lookup_limit": 20000,
            "dedup_window": 600,
            "max_bitrate": 200000000,
            "path_mappings": "",
            "scan_roots": "",
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页，展示最近的预热记录。"""
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "暂无预热记录"},
            }]
        status_text = {"success": "成功", "fail": "失败", "skip": "跳过", "changed": "换源"}
        status_color = {"success": "success", "fail": "error", "skip": "grey", "changed": "warning"}
        rows = []
        for record in history[:100]:
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "text": record.get("time") or ""},
                    {"component": "td", "text": record.get("source") or ""},
                    {"component": "td", "text": record.get("title") or ""},
                    {"component": "td", "content": [{
                        "component": "VChip",
                        "props": {
                            "size": "small",
                            "color": status_color.get(record.get("status"), "grey"),
                            "variant": "tonal",
                        },
                        "text": status_text.get(record.get("status"), record.get("status") or ""),
                    }]},
                    {"component": "td", "text": record.get("detail") or ""},
                    {"component": "td", "props": {"class": "text-caption"}, "text": record.get("path") or ""},
                ],
            })
        return [{
            "component": "VTable",
            "props": {"hover": True, "density": "compact"},
            "content": [
                {
                    "component": "thead",
                    "content": [{
                        "component": "tr",
                        "content": [
                            {"component": "th", "text": "时间"},
                            {"component": "th", "text": "来源"},
                            {"component": "th", "text": "标题"},
                            {"component": "th", "text": "状态"},
                            {"component": "th", "text": "媒体信息"},
                            {"component": "th", "text": "路径"},
                        ],
                    }],
                },
                {"component": "tbody", "content": rows},
            ],
        }]
