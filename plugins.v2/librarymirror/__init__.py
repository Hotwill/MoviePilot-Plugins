"""媒体库云盘镜像插件。

入库整理完成后，把媒体库中的成品文件按**相同的目录结构**再复制一份到
OpenList/AList 等云盘存储，例如：

    媒体库  /media/电视剧/国产剧/兰香如故 (2026)/Season 01/兰香如故 - S01E01.mkv
    云盘    /cloud/电视剧/国产剧/兰香如故 (2026)/Season 01/兰香如故 - S01E01.mkv

MoviePilot 原生一次整理只会落到一个媒体库目录，本插件用于补齐「本地一份、
云盘一份」的双写需求。
"""

import os
import queue
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field

# 宿主导入：优先使用 V3 稳定 SDK，仅当 SDK 模块本身不存在时才回退 V2 旧路径，
# 避免宿主第三方依赖缺失被误判成「没有 V3 SDK」而掩盖真实错误。
try:  # MoviePilot V3
    from app.sdk.events import Event, eventmanager
    from app.sdk.logging import logger
    from app.sdk.services import StorageHelper
    from app.application.directory import DirectoryHelper
except ImportError as _sdk_error:  # MoviePilot V2
    _missing = getattr(_sdk_error, "name", "") or ""
    if not (_missing.startswith("app.sdk") or _missing.startswith("app.application")
            or "app.sdk" in str(_sdk_error)):
        raise
    from app.core.event import Event, eventmanager
    from app.log import logger
    from app.helper.storage import StorageHelper
    from app.helper.directory import DirectoryHelper

from app.chain.storage import StorageChain
from app.plugins import _PluginBase
from app.schemas import FileItem
from app.schemas.types import EventType

try:  # V3 使用 MessageType
    from app.schemas.types import MessageType as _MsgType
except ImportError:  # V2 使用 NotificationType
    from app.schemas.types import NotificationType as _MsgType

# 默认镜像的媒体文件扩展名
DEFAULT_MEDIA_EXTENSIONS = (
    ".mkv", ".mp4", ".ts", ".iso", ".avi", ".wmv", ".m2ts", ".mpg", ".mpeg",
    ".flv", ".rmvb", ".mov", ".m4v", ".webm",
)
# 默认一起镜像的同名附属文件扩展名（刮削与字幕）
DEFAULT_SIDECAR_EXTENSIONS = (
    ".nfo", ".srt", ".ass", ".ssa", ".sub", ".sup", ".idx", ".vtt",
    "-thumb.jpg", "-poster.jpg", "-fanart.jpg",
)
# 默认一起镜像的目录级图片文件名
DEFAULT_SIDECAR_NAMES = (
    "poster.jpg", "fanart.jpg", "banner.jpg", "clearlogo.png", "logo.png",
    "thumb.jpg", "landscape.jpg", "tvshow.nfo", "season.nfo",
)


class ApiResult(BaseModel):
    """插件 API 的统一返回结构，与宿主三段式响应保持一致。"""

    # 请求或业务操作是否成功
    success: bool
    # 给调用方展示的说明文本
    message: str = ""
    # 业务数据
    data: Dict[str, Any] = Field(default_factory=dict)


def human_size(size: Optional[int]) -> str:
    """把字节数格式化为人类可读的大小。"""
    if not size:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.2f}{unit}"
        value /= 1024
    return f"{value:.2f}TB"


def human_elapsed(seconds: float) -> str:
    """把秒数格式化为「x分y秒」或「x.y秒」。"""
    if seconds < 60:
        return f"{seconds:.1f}秒"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}分{rest}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分"


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


def parse_lines(text: str) -> Tuple[str, ...]:
    """把多行配置解析成去空白、去注释的元组。"""
    return tuple(
        line.strip() for line in (text or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


def parse_extensions(text: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
    """解析扩展名配置，支持逗号或换行分隔，缺省时返回默认值。"""
    raw = (text or "").replace("\n", ",")
    items = tuple(
        item.strip().lower() if item.strip().startswith(("-", ".")) else f".{item.strip().lower()}"
        for item in raw.split(",") if item.strip()
    )
    return items or default


def normalize_remote_path(path: str) -> str:
    """把云盘目录规范化为以 / 开头、不以 / 结尾的绝对路径。"""
    path = (path or "").strip().replace("\\", "/")
    if not path:
        return ""
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def relative_to_root(file_path: str, roots: Tuple[str, ...]) -> Optional[str]:
    """在候选媒体库根目录中找出最长匹配，返回相对路径（POSIX 风格）。

    例如根目录 ``/media`` 与文件 ``/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv``
    会得到 ``电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv``，云盘侧据此保持同样结构。
    """
    if not file_path:
        return None
    target = Path(file_path.replace("\\", "/"))
    best: Optional[str] = None
    for root in sorted(roots, key=len, reverse=True):
        root_path = Path((root or "").replace("\\", "/").rstrip("/") or "/")
        try:
            relative = target.relative_to(root_path)
        except ValueError:
            continue
        candidate = relative.as_posix()
        if candidate and (best is None or len(candidate) < len(best)):
            best = candidate
    return best


def is_media_file(path: str, extensions: Tuple[str, ...]) -> bool:
    """判断文件是否属于需要镜像的媒体文件。"""
    if not path:
        return False
    return Path(path).suffix.lower() in extensions


def sidecar_candidates(file_path: str, extensions: Tuple[str, ...],
                       names: Tuple[str, ...]) -> List[str]:
    """列出与媒体文件一起镜像的本地附属文件。

    包含两类：与媒体同名的刮削/字幕文件（含 ``a.zh.srt`` 这类多段后缀），
    以及同目录下的固定名图片与剧集级 NFO。
    """
    media = Path(file_path)
    parent = media.parent
    if not parent.is_dir():
        return []
    stem = media.stem
    found: List[str] = []
    for entry in sorted(parent.iterdir()):
        if not entry.is_file() or entry.name == media.name:
            continue
        lower = entry.name.lower()
        if entry.stem == stem or entry.name.startswith(f"{stem}."):
            if any(lower.endswith(extension) for extension in extensions):
                found.append(str(entry))
                continue
        if lower in names:
            found.append(str(entry))
    return found


class LibraryMirror(_PluginBase):
    """把媒体库成品文件按相同目录结构镜像到云盘存储的插件主类。"""

    # 插件名称
    plugin_name = "媒体库云盘镜像"
    # 插件描述
    plugin_desc = "入库后把媒体库文件按相同目录结构再复制一份到OpenList/AList等云盘，实现本地与云盘双写。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Hotwill/MoviePilot-Plugins/main/icons/librarymirror.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "Hotwill"
    # 作者主页
    author_url = "https://github.com/Hotwill"
    # 插件配置项ID前缀
    plugin_config_prefix = "librarymirror_"
    # 加载顺序
    plugin_order = 16
    # 可使用的用户级别
    auth_level = 1

    def __init__(self):
        """初始化运行期属性，真实配置在 init_plugin 中读取。"""
        super().__init__()
        self._enabled = False
        self._notify = False
        self._notify_success = False
        self._target_storage = ""
        self._target_root = ""
        self._source_roots: Tuple[str, ...] = ()
        self._media_extensions: Tuple[str, ...] = DEFAULT_MEDIA_EXTENSIONS
        self._sidecar_extensions: Tuple[str, ...] = DEFAULT_SIDECAR_EXTENSIONS
        self._sidecar_names: Tuple[str, ...] = DEFAULT_SIDECAR_NAMES
        self._copy_sidecars = True
        self._copy_folder_images = False
        self._skip_strm = True
        self._overwrite = "size"
        self._delay = 5
        self._max_retries = 2
        self._retry_interval = 30
        self._include_types: List[str] = []
        self._include_categories: Tuple[str, ...] = ()
        self._cron = ""
        self._onlyonce = False
        self._max_items = 0
        self._item_interval = 1.0
        self._history_count = 200
        # 运行期资源
        self._queue: "queue.Queue[dict]" = queue.Queue()
        self._stop_event = threading.Event()
        self._workers: List[threading.Thread] = []
        self._lock = threading.Lock()
        self._inflight: set = set()
        self._scanning = False

    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建后台工作线程，可重复调用。"""
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify"))
        self._notify_success = bool(config.get("notify_success"))
        self._target_storage = (config.get("target_storage") or "").strip()
        self._target_root = normalize_remote_path(config.get("target_root"))
        self._source_roots = parse_lines(config.get("source_roots"))
        self._media_extensions = parse_extensions(config.get("media_extensions"), DEFAULT_MEDIA_EXTENSIONS)
        self._sidecar_extensions = parse_extensions(config.get("sidecar_extensions"), DEFAULT_SIDECAR_EXTENSIONS)
        self._sidecar_names = DEFAULT_SIDECAR_NAMES
        self._copy_sidecars = bool(config.get("copy_sidecars", True))
        self._copy_folder_images = bool(config.get("copy_folder_images"))
        self._skip_strm = bool(config.get("skip_strm", True))
        self._overwrite = config.get("overwrite") or "size"
        self._delay = self._to_int(config.get("delay"), 5)
        self._max_retries = self._to_int(config.get("max_retries"), 2)
        self._retry_interval = self._to_int(config.get("retry_interval"), 30)
        self._include_types = config.get("include_types") or []
        self._include_categories = parse_lines(config.get("include_categories"))
        self._cron = (config.get("cron") or "").strip()
        self._onlyonce = bool(config.get("onlyonce"))
        self._max_items = self._to_int(config.get("max_items"), 0)
        self._item_interval = max(0.0, self._to_float(config.get("item_interval"), 1.0))
        self._history_count = max(20, self._to_int(config.get("history_count"), 200))

        # 重置停止标记与任务队列，保证重载后不残留上一轮状态
        self._stop_event = threading.Event()
        self._queue = queue.Queue()

        if not self._enabled:
            return
        if not self._target_storage or not self._target_root:
            logger.warning("媒体库云盘镜像未配置目标存储或云盘根目录，插件不会执行")
            return

        worker = threading.Thread(target=self._worker_loop, name="LibraryMirrorWorker", daemon=True)
        worker.start()
        self._workers = [worker]
        logger.info(f"媒体库云盘镜像已启动，目标：{self._target_storage}:{self._target_root}")

        if self._onlyonce:
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
            "target_storage": self._target_storage,
            "target_root": self._target_root,
            "source_roots": "\n".join(self._source_roots),
            "media_extensions": ",".join(self._media_extensions),
            "sidecar_extensions": ",".join(self._sidecar_extensions),
            "copy_sidecars": self._copy_sidecars,
            "copy_folder_images": self._copy_folder_images,
            "skip_strm": self._skip_strm,
            "overwrite": self._overwrite,
            "delay": self._delay,
            "max_retries": self._max_retries,
            "retry_interval": self._retry_interval,
            "include_types": self._include_types,
            "include_categories": "\n".join(self._include_categories),
            "cron": self._cron,
            "onlyonce": False,
            "max_items": self._max_items,
            "item_interval": self._item_interval,
            "history_count": self._history_count,
        }

    def get_state(self) -> bool:
        """返回插件是否启用。"""
        return bool(self._enabled and self._target_storage and self._target_root)

    def library_roots(self) -> Tuple[str, ...]:
        """返回用于计算相对路径的媒体库根目录。

        优先使用插件配置；留空时自动读取 MoviePilot「目录配置」里的媒体库目录，
        这样云盘侧就能保持 ``电视剧/国产剧/剧名 (年份)/Season 01`` 这种结构。
        """
        if self._source_roots:
            return self._source_roots
        roots = []
        try:
            for directory in DirectoryHelper().get_dirs():
                library_path = getattr(directory, "library_path", None)
                if library_path:
                    roots.append(str(library_path).rstrip("/") or "/")
        except Exception as err:
            logger.warning(f"读取目录配置失败，请在插件中手动填写媒体库根目录：{err}")
        return tuple(dict.fromkeys(roots))

    def target_path(self, local_path: str) -> Optional[str]:
        """把媒体库文件路径换算成云盘目标路径，保持目录结构不变。"""
        relative = relative_to_root(local_path, self.library_roots())
        if not relative:
            return None
        root = self._target_root.rstrip("/")
        return f"{root}/{relative}" if root != "/" else f"/{relative}"

    # ------------------------------------------------------------------ 云盘操作

    def _remote_item(self, remote_path: str) -> Optional[FileItem]:
        """查询云盘上的文件项，不存在返回 None。"""
        try:
            return StorageChain().get_file_item(storage=self._target_storage, path=Path(remote_path))
        except Exception as err:
            logger.debug(f"查询云盘文件 {remote_path} 失败：{err}")
            return None

    def _remote_folder(self, remote_dir: str) -> Optional[FileItem]:
        """获取云盘目录项，不存在则逐级创建。

        不能只依赖 ``StorageChain.get_folder``：MoviePilot v2.11.3 等版本的
        文件管理模块并未实现 ``get_folder``，链路会静默返回 None。因此这里先用
        已存在判断，再尝试宿主的 get_folder，最后回退到 ``create_folder`` 逐级创建。
        """
        existing = self._remote_item(remote_dir)
        if existing is not None:
            return existing
        chain = StorageChain()
        getter = getattr(chain, "get_folder", None)
        if getter:
            try:
                folder = getter(storage=self._target_storage, path=Path(remote_dir))
            except Exception as err:
                logger.debug(f"宿主 get_folder 调用失败，改用逐级创建：{err}")
                folder = None
            if folder is not None:
                return folder
        return self._create_folders(remote_dir)

    def _create_folders(self, remote_dir: str) -> Optional[FileItem]:
        """按层级逐个创建云盘目录，返回最终目录项。"""
        chain = StorageChain()
        parts = [part for part in PurePosixPath(remote_dir).parts if part not in ("/", "")]
        parent = self._remote_item("/") or FileItem(
            storage=self._target_storage, type="dir", path="/", name="/")
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            existing = self._remote_item(current)
            if existing is not None:
                parent = existing
                continue
            try:
                created = chain.create_folder(fileitem=parent, name=part)
            except Exception as err:
                logger.error(f"创建云盘目录 {current} 失败：{err}")
                return None
            if not created:
                logger.error(f"创建云盘目录 {current} 失败，请检查 OpenList 该路径是否可写")
                return None
            parent = created
        return parent

    def should_upload(self, local_path: str, remote: Optional[FileItem]) -> Tuple[bool, str]:
        """按覆盖策略判断是否需要上传，返回 (是否上传, 原因)。"""
        if remote is None:
            return True, "云盘不存在"
        if self._overwrite == "always":
            return True, "按配置总是覆盖"
        if self._overwrite == "never":
            return False, "云盘已存在"
        # size 策略：大小一致视为同一文件
        try:
            local_size = Path(local_path).stat().st_size
        except OSError:
            return False, "本地文件不可读"
        remote_size = getattr(remote, "size", None)
        if remote_size and int(remote_size) == local_size:
            return False, f"云盘已存在且大小一致({local_size})"
        return True, f"大小不一致(本地{local_size} 云盘{remote_size})"

    def _local_copy(self, item: FileItem) -> Tuple[Optional[str], Optional[str]]:
        """拿到可用于上传的本地文件路径。

        媒体库在本地时直接返回原路径；在远端存储时先下载到临时文件，
        返回值第二项为需要在上传后删除的临时路径。
        """
        storage = getattr(item, "storage", None) or "local"
        path = getattr(item, "path", None)
        if not path:
            return None, None
        if storage == "local":
            return str(path), None
        temporary = Path(tempfile.mkdtemp(prefix="library-mirror-")) / Path(str(path)).name
        try:
            downloaded = StorageChain().download_file(item, temporary)
        except Exception as err:
            logger.error(f"从 {storage} 下载 {path} 失败：{err}")
            downloaded = None
        if not downloaded:
            shutil.rmtree(temporary.parent, ignore_errors=True)
            return None, None
        return str(downloaded), str(temporary.parent)

    def _upload(self, item: FileItem, remote_path: str) -> Tuple[bool, str]:
        """把单个文件上传到云盘目标路径。"""
        folder = self._remote_folder(str(Path(remote_path).parent))
        if not folder:
            return False, "云盘目录创建失败"
        local_path, temporary_dir = self._local_copy(item)
        if not local_path:
            return False, "无法获取本地文件"
        try:
            uploaded = StorageChain().upload_file(
                fileitem=folder, path=Path(local_path), new_name=Path(remote_path).name)
        except Exception as err:
            return False, f"{type(err).__name__}: {err}"
        finally:
            if temporary_dir:
                shutil.rmtree(temporary_dir, ignore_errors=True)
        if not uploaded:
            return False, "上传失败"
        return True, "上传完成"

    # ------------------------------------------------------------------ 镜像流程

    def mirror_file(self, item: FileItem, title: str, source: str,
                    image: str = "") -> Optional[dict]:
        """镜像单个媒体文件（含附属文件），返回历史记录。"""
        local_path = str(getattr(item, "path", "") or "")
        if not local_path:
            return None
        if self._skip_strm and local_path.lower().endswith(".strm"):
            logger.debug(f"{local_path} 是 STRM 文件，按配置跳过镜像")
            return None
        if not is_media_file(local_path, self._media_extensions):
            logger.debug(f"{local_path} 不在镜像扩展名范围内，跳过")
            return None
        remote_path = self.target_path(local_path)
        if not remote_path:
            logger.warning(f"{local_path} 不在任何媒体库根目录下，无法计算云盘路径，"
                           f"请检查插件的「媒体库根目录」配置")
            return {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title or Path(local_path).name,
                "path": local_path,
                "remote": "",
                "source": source,
                "status": "fail",
                "detail": "未匹配到媒体库根目录",
                "image": image or "",
                "filename": Path(local_path).name,
            }

        try:
            file_size = Path(local_path).stat().st_size
        except OSError:
            file_size = 0
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "title": title or Path(local_path).name,
            "path": local_path,
            "remote": remote_path,
            "source": source,
            "status": "skip",
            "detail": "",
            "size": file_size,
            "image": image or "",
            "filename": Path(local_path).name,
        }
        needed, reason = self.should_upload(local_path, self._remote_item(remote_path))
        if not needed:
            record["detail"] = reason
            logger.info(f"跳过 {Path(local_path).name}：{reason}")
            return record

        started = time.time()
        error = ""
        for attempt in range(self._max_retries + 1):
            if self._stop_event.is_set():
                record.update(status="fail", detail="插件已停止")
                return record
            ok, message = self._upload(item, remote_path)
            if ok:
                elapsed = time.time() - started
                extras = self._mirror_sidecars(local_path) if self._copy_sidecars else 0
                record.update(
                    status="success",
                    elapsed=round(elapsed, 1),
                    extras=extras,
                    detail=f"{reason} → 已上传 用时{human_elapsed(elapsed)}"
                           + (f" 附属文件{extras}个" if extras else ""),
                )
                logger.info(f"镜像成功 {remote_path} 用时{human_elapsed(elapsed)}"
                            + (f"，附属文件 {extras} 个" if extras else ""))
                return record
            error = message
            if attempt < self._max_retries:
                logger.warning(f"镜像失败 {remote_path}：{error}，{self._retry_interval}s 后重试")
                if self._stop_event.wait(self._retry_interval):
                    break
        record.update(status="fail", detail=error)
        logger.error(f"镜像失败 {remote_path}：{error}")
        return record

    def _mirror_sidecars(self, local_media_path: str) -> int:
        """镜像同名刮削、字幕以及（可选）目录图片，返回成功数量。"""
        names = self._sidecar_names if self._copy_folder_images else ()
        count = 0
        for sidecar in sidecar_candidates(local_media_path, self._sidecar_extensions, names):
            remote_path = self.target_path(sidecar)
            if not remote_path:
                continue
            needed, _reason = self.should_upload(sidecar, self._remote_item(remote_path))
            if not needed:
                continue
            item = FileItem(storage="local", type="file", path=sidecar, name=Path(sidecar).name)
            ok, message = self._upload(item, remote_path)
            if ok:
                count += 1
            else:
                logger.warning(f"附属文件镜像失败 {remote_path}：{message}")
        return count

    def matches_filters(self, media_type: Optional[str], category: Optional[str]) -> bool:
        """按配置的媒体类型与二级分类判断是否需要镜像。"""
        if self._include_types and media_type and media_type not in self._include_types:
            return False
        if self._include_categories and (category or "") not in self._include_categories:
            return False
        return True

    # ------------------------------------------------------------------ 任务调度

    def _worker_loop(self) -> None:
        """后台工作线程：串行消费镜像任务，避免阻塞入库流程。"""
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
                logger.error(f"处理镜像任务出错：{err}")
            finally:
                self._queue.task_done()

    def _handle_task(self, task: dict) -> None:
        """处理一次入库镜像任务。"""
        delay = self._delay if task.get("delay") is None else self._to_int(task.get("delay"), self._delay)
        if delay > 0 and self._stop_event.wait(delay):
            return
        records = []
        for path in task.get("paths") or []:
            if self._stop_event.is_set():
                break
            with self._lock:
                if path in self._inflight:
                    continue
                self._inflight.add(path)
            try:
                item = FileItem(storage=task.get("storage") or "local", type="file",
                                path=path, name=Path(path).name)
                record = self.mirror_file(item, task.get("title") or "",
                                          task.get("source") or "入库",
                                          image=task.get("image") or "")
                if record:
                    records.append(record)
            finally:
                with self._lock:
                    self._inflight.discard(path)
        if records:
            self._record_history(records)
            self._notify_records(records)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event) -> None:
        """监听整理入库完成事件，把媒体库成品文件镜像到云盘。"""
        if not self.get_state():
            return
        event_data = event.event_data or {}
        transferinfo = event_data.get("transferinfo")
        mediainfo = event_data.get("mediainfo")
        if not transferinfo:
            return
        media_type = getattr(getattr(mediainfo, "type", None), "value", None) or \
            (str(getattr(mediainfo, "type", "")) if mediainfo else None)
        category = getattr(mediainfo, "category", None)
        if not self.matches_filters(media_type, category):
            logger.debug(f"{getattr(mediainfo, 'title', '')} 不在镜像范围内（类型/分类过滤）")
            return

        paths: List[str] = []
        target_item = getattr(transferinfo, "target_item", None)
        storage = getattr(target_item, "storage", None) or "local"
        if target_item is not None and getattr(target_item, "path", None):
            paths.append(str(target_item.path))
        for path in (getattr(transferinfo, "file_list_new", None) or []):
            if str(path) not in paths:
                paths.append(str(path))
        paths = [path for path in paths if is_media_file(path, self._media_extensions)]
        if not paths:
            return
        title = getattr(mediainfo, "title_year", None) or getattr(mediainfo, "title", None) or ""
        self._queue.put({
            "type": "files",
            "paths": paths,
            "storage": storage,
            "title": title,
            "source": "入库",
            "image": media_image(mediainfo),
        })
        logger.info(f"已加入云盘镜像队列：{title or paths[0]}（{len(paths)} 个文件）")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """响应远程命令，手动触发一次全量补漏镜像。"""
        if not self.get_state():
            return
        if (event.event_data or {}).get("action") != "library_mirror":
            return
        self._queue.put({"type": "scan", "source": "命令"})

    def full_scan(self, source: str = "定时") -> None:
        """遍历媒体库根目录，把云盘缺失的文件补齐。"""
        if self._scanning:
            logger.info("已有扫描任务在执行，跳过本次全量镜像")
            return
        roots = [root for root in self.library_roots() if Path(root).is_dir()]
        if not roots:
            logger.warning("没有可访问的本地媒体库根目录，跳过全量镜像")
            return
        self._scanning = True
        records = []
        try:
            handled = 0
            for root in roots:
                for directory, _dirs, files in os.walk(root):
                    if self._stop_event.is_set():
                        break
                    for name in sorted(files):
                        if self._stop_event.is_set():
                            break
                        local_path = str(Path(directory) / name)
                        if not is_media_file(local_path, self._media_extensions):
                            continue
                        item = FileItem(storage="local", type="file", path=local_path, name=name)
                        record = self.mirror_file(item, name, source)
                        if record:
                            records.append(record)
                        if record and record.get("status") != "skip":
                            handled += 1
                            if self._max_items and handled >= self._max_items:
                                logger.info(f"已达单次处理上限 {self._max_items}，本轮结束")
                                return
                            if self._item_interval and self._stop_event.wait(self._item_interval):
                                return
        finally:
            self._scanning = False
            if records:
                self._record_history(records)
                handled_records = [record for record in records if record.get("status") != "skip"]
                if handled_records:
                    self._notify_records(handled_records, summary=True)

    def _record_history(self, records: List[dict]) -> None:
        """写入运行历史，只保留最近若干条。"""
        history = self.get_data("history") or []
        self.save_data("history", (list(records) + list(history))[:self._history_count])

    @staticmethod
    def _detail_text(record: dict) -> str:
        """拼装单条记录的通知正文，风格贴近 MoviePilot 的入库通知。"""
        lines = []
        if record.get("filename"):
            lines.append(f"📄 文件：{record['filename']}")
        if record.get("size"):
            lines.append(f"📦 大小：{human_size(record.get('size'))}")
        if record.get("remote"):
            lines.append(f"☁️ 云盘：{record['remote']}")
        if record.get("elapsed"):
            lines.append(f"⏱️ 耗时：{human_elapsed(float(record['elapsed']))}")
        if record.get("extras"):
            lines.append(f"🧩 附属文件：{record['extras']} 个")
        if record.get("status") == "fail" and record.get("detail"):
            lines.append(f"⚠️ 原因：{record['detail']}")
        if record.get("source"):
            lines.append(f"🏷️ 触发：{record['source']}")
        return "\n".join(lines)

    def _notify_records(self, records: List[dict], summary: bool = False) -> None:
        """按配置推送镜像结果通知，带媒体图片与分行排版。"""
        if not self._notify or not records:
            return
        success = [record for record in records if record.get("status") == "success"]
        failed = [record for record in records if record.get("status") == "fail"]
        if not failed and not (success and self._notify_success):
            return
        shown = success + failed
        image = next((record.get("image") for record in shown if record.get("image")), "")
        if len(shown) == 1:
            record = shown[0]
            flag = "✅ 已镜像到云盘" if record.get("status") == "success" else "❌ 云盘镜像失败"
            self.post_message(
                mtype=_MsgType.Plugin,
                title=f"{record.get('title') or '媒体文件'} {flag}",
                text=self._detail_text(record),
                image=image or None,
            )
            return
        # 多条时给出汇总 + 明细清单
        header = [f"✅ 成功 {len(success)} 个"] if success else []
        if failed:
            header.append(f"❌ 失败 {len(failed)} 个")
        lines = [" · ".join(header)] if header else []
        for record in shown[:15]:
            flag = {"success": "✅", "fail": "❌"}.get(record.get("status"), "➖")
            detail = record.get("detail") if record.get("status") == "fail" else human_size(record.get("size"))
            lines.append(f"{flag} {record.get('title')}" + (f"（{detail}）" if detail else ""))
        if len(shown) > 15:
            lines.append(f"…… 其余 {len(shown) - 15} 个见插件详情页")
        title = "媒体库云盘镜像完成" if not failed else (
            "媒体库云盘镜像失败" if not success else f"媒体库云盘镜像完成，失败 {len(failed)} 个")
        self.post_message(mtype=_MsgType.Plugin, title=title,
                          text="\n".join(lines), image=image or None)

    def stop_service(self) -> None:
        """停止后台线程并释放资源，可重复调用。"""
        self._stop_event.set()
        for worker in self._workers:
            if worker.is_alive():
                worker.join(timeout=5)
        self._workers = []
        with self._lock:
            self._inflight.clear()

    # ------------------------------------------------------------------ 宿主接口

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册手动触发全量补漏镜像的远程命令。"""
        return [
            {
                "cmd": "/library_mirror",
                "event": EventType.PluginAction,
                "desc": "媒体库云盘镜像",
                "category": "插件命令",
                "data": {"action": "library_mirror"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """按配置注册定时全量补漏镜像任务。"""
        if not self.get_state() or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(f"定时镜像周期配置错误：{err}")
            return []
        return [
            {
                "id": "LibraryMirror.FullScan",
                "name": "媒体库云盘镜像补漏扫描",
                "trigger": trigger,
                "func": self.full_scan,
                "kwargs": {},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件 API：查询状态与历史、外部触发镜像。"""
        return [
            {
                "path": "/status",
                "endpoint": self.api_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询云盘镜像状态",
                "response_model": ApiResult,
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查询云盘镜像历史",
                "response_model": ApiResult,
            },
            {
                "path": "/mirror",
                "endpoint": self.api_mirror,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "外部触发云盘镜像",
                "response_model": ApiResult,
            },
        ]

    def api_status(self) -> ApiResult:
        """返回插件运行状态。"""
        history = self.get_data("history") or []
        counts: Dict[str, int] = {}
        for record in history:
            status = str(record.get("status") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return ApiResult(success=True, data={
            "enabled": self.get_state(),
            "target_storage": self._target_storage,
            "target_root": self._target_root,
            "library_roots": list(self.library_roots()),
            "queued": self._queue.qsize(),
            "scanning": self._scanning,
            "overwrite": self._overwrite,
            "history_counts": counts,
            "last_record": history[0] if history else None,
        })

    def api_history(self, limit: int = 50) -> ApiResult:
        """返回最近的镜像记录。"""
        history = self.get_data("history") or []
        limit = max(1, min(int(limit or 50), self._history_count))
        return ApiResult(success=True, data={"total": len(history), "records": history[:limit]})

    def api_mirror(self, path: str = None, delay: int = None) -> ApiResult:
        """外部触发镜像指定的媒体库文件。"""
        if not self.get_state():
            return ApiResult(success=False, message="插件未启用或未配置云盘目标")
        if not path:
            return ApiResult(success=False, message="缺少参数：path")
        remote_path = self.target_path(path)
        if not remote_path:
            return ApiResult(success=False, message="路径不在任何媒体库根目录下")
        self._queue.put({
            "type": "files",
            "paths": [path],
            "storage": "local",
            "title": Path(path).name,
            "source": "API",
            "delay": delay,
        })
        return ApiResult(success=True, message="已加入镜像队列",
                         data={"path": path, "remote": remote_path, "queued": self._queue.qsize()})

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置页面与默认配置模型。"""
        try:
            storage_items = [
                {"title": f"{conf.name or conf.type}（{conf.type}）", "value": conf.type}
                for conf in StorageHelper().get_storagies() if conf.type and conf.type != "local"
            ]
        except Exception:
            storage_items = []
        if not storage_items:
            storage_items = [{"title": "OpenList/AList（alist）", "value": "alist"},
                             {"title": "AListGo（alistgo）", "value": "alistgo"}]

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

        def area(model: str, label: str, placeholder: str, hint: str, cols: int = 6) -> dict:
            """生成一个多行输入配置项。"""
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": cols},
                "content": [{
                    "component": "VTextarea",
                    "props": {"model": model, "label": label, "rows": 4, "placeholder": placeholder,
                              "hint": hint, "persistent-hint": True},
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
                    "props": {"cols": 12, "md": 4},
                    "content": [{
                        "component": "VSelect",
                        "props": {
                            "model": "target_storage", "label": "云盘存储",
                            "items": storage_items,
                            "hint": "需先在「存储」中配置 OpenList/AList",
                            "persistent-hint": True,
                        },
                    }],
                },
                    text("target_root", "云盘根目录", "/cloud/media",
                         "媒体库结构会原样挂在该目录下"),
                    {
                        "component": "VCol",
                        "props": {"cols": 12, "md": 4},
                        "content": [{
                            "component": "VSelect",
                            "props": {
                                "model": "overwrite", "label": "覆盖策略",
                                "items": [
                                    {"title": "大小不同才覆盖", "value": "size"},
                                    {"title": "已存在则跳过", "value": "never"},
                                    {"title": "总是覆盖", "value": "always"},
                                ],
                            },
                        }],
                    }]),
                row([
                    switch("copy_sidecars", "复制同名刮削/字幕", "nfo、srt、ass 等同名文件"),
                    switch("copy_folder_images", "复制目录图片", "poster.jpg、fanart.jpg 等"),
                    switch("skip_strm", "跳过 STRM 文件", "STRM 指向直链，上传云盘无意义"),
                ]),
                row([
                    text("delay", "入库后延迟（秒）", "5"),
                    text("max_retries", "失败重试次数", "2"),
                    text("retry_interval", "重试间隔（秒）", "30"),
                ]),
                row([
                    text("cron", "定时补漏扫描", "0 4 * * *", "留空则不执行定时全量镜像"),
                    text("max_items", "单次最多处理", "0", "0 表示不限制"),
                    text("item_interval", "文件间隔（秒）", "1"),
                ]),
                row([{
                    "component": "VCol",
                    "props": {"cols": 12, "md": 8},
                    "content": [{
                        "component": "VSelect",
                        "props": {
                            "multiple": True, "chips": True, "clearable": True,
                            "model": "include_types", "label": "只镜像这些媒体类型",
                            "items": [{"title": "电影", "value": "电影"}, {"title": "电视剧", "value": "电视剧"}],
                            "hint": "留空表示全部类型",
                            "persistent-hint": True,
                        },
                    }],
                }, switch("onlyonce", "立即运行一次", "保存后立即执行一次全量补漏")]),
                row([
                    area("source_roots", "媒体库根目录", "/media\n/media2",
                         "留空自动读取「目录配置」中的媒体库目录；相对路径由此计算"),
                    area("include_categories", "只镜像这些二级分类", "国产剧\n动漫",
                         "对应 MoviePilot 的媒体类别，留空表示全部"),
                ]),
                row([
                    area("media_extensions", "镜像的媒体扩展名",
                         ".mkv,.mp4,.ts", "留空使用默认视频扩展名"),
                    area("sidecar_extensions", "同名附属文件扩展名",
                         ".nfo,.srt,.ass", "留空使用默认刮削与字幕扩展名"),
                ]),
                row([{
                    "component": "VCol",
                    "props": {"cols": 12},
                    "content": [{
                        "component": "VAlert",
                        "props": {
                            "type": "info", "variant": "tonal",
                            "text": "目录结构示例：媒体库 /media/电视剧/国产剧/兰香如故 (2026)/Season 01/xxx.mkv，"
                                    "云盘根目录填 /cloud/media 时，云盘路径为 "
                                    "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01/xxx.mkv。"
                                    "整理方式建议用 copy 或 link（move 会导致源文件消失，但媒体库那份仍可镜像）。",
                        },
                    }],
                }]),
            ],
        }], {
            "enabled": False,
            "notify": False,
            "notify_success": False,
            "target_storage": "",
            "target_root": "",
            "overwrite": "size",
            "copy_sidecars": True,
            "copy_folder_images": False,
            "skip_strm": True,
            "delay": 5,
            "max_retries": 2,
            "retry_interval": 30,
            "cron": "",
            "max_items": 0,
            "item_interval": 1,
            "include_types": [],
            "onlyonce": False,
            "source_roots": "",
            "include_categories": "",
            "media_extensions": "",
            "sidecar_extensions": "",
            "history_count": 200,
        }

    def get_page(self) -> List[dict]:
        """返回插件详情页，展示最近的镜像记录。"""
        history = self.get_data("history") or []
        if not history:
            return [{
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "暂无镜像记录"},
            }]
        status_text = {"success": "已上传", "fail": "失败", "skip": "跳过"}
        status_color = {"success": "success", "fail": "error", "skip": "grey"}
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
                        "props": {"size": "small",
                                  "color": status_color.get(record.get("status"), "grey"),
                                  "variant": "tonal"},
                        "text": status_text.get(record.get("status"), record.get("status") or ""),
                    }]},
                    {"component": "td", "props": {"class": "text-caption"}, "text": record.get("remote") or ""},
                    {"component": "td", "text": record.get("detail") or ""},
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
                            {"component": "th", "text": "云盘路径"},
                            {"component": "th", "text": "说明"},
                        ],
                    }],
                },
                {"component": "tbody", "content": rows},
            ],
        }]
