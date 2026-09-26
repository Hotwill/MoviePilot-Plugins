"""针对真实 HTTP 交互的集成测试。

单元测试用假 HTTP 客户端覆盖逻辑分支，本文件改为启动一个模拟 Emby 的
本地 HTTP 服务，用真实的 requests 调用验证 URL 拼接、查询参数、POST 正文
以及「PlaybackInfo 之后媒体信息补全」的完整链路。
"""

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin_loader import load_plugin  # noqa: E402

plugin_module = load_plugin("strmprewarmer")

INCOMPLETE = {"MediaSources": [{"MediaStreams": [{"Type": "Video", "Codec": "hevc"}]}]}
COMPLETE = {"MediaSources": [{"MediaStreams": [
    {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "BitRate": 20000000},
    {"Type": "Audio", "Codec": "eac3"},
]}]}


class FakeEmbyState:
    """记录模拟 Emby 服务收到的请求与条目状态。"""

    def __init__(self) -> None:
        self.requests = []
        self.probed = False
        self.item_path = "/data/media/movies/demo.strm"
        self.fail_playbackinfo = 0
        self.support_path_query = True

    def item(self) -> dict:
        """返回当前条目详情，探测后带完整媒体信息。"""
        base = {"Id": "1001", "Name": "Demo", "Type": "Movie", "Path": self.item_path}
        base.update(COMPLETE if self.probed else INCOMPLETE)
        return base


class Handler(BaseHTTPRequestHandler):
    """把请求路由到模拟的 Emby API。"""

    state: FakeEmbyState = None

    def log_message(self, *args) -> None:
        """关闭默认访问日志。"""

    def _send(self, payload, status: int = 200) -> None:
        """返回 JSON 响应。"""
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        """处理条目查询与条目详情。"""
        parsed = urlparse(self.path)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        self.state.requests.append(("GET", parsed.path, query))
        if query.get("api_key") != "test-key":
            self._send({"message": "Requires authentication"}, 401)
            return
        if parsed.path == "/emby/Items":
            if "Path" in query:
                if not self.state.support_path_query:
                    self._send({"Items": [], "TotalRecordCount": 0})
                    return
                items = [self.state.item()] if query["Path"] == self.state.item_path else []
                self._send({"Items": items, "TotalRecordCount": len(items)})
                return
            if "StartIndex" in query:
                items = [self.state.item()] if query["StartIndex"] == "0" else []
                self._send({"Items": items, "TotalRecordCount": len(items)})
                return
            self._send({"Items": [], "TotalRecordCount": 0})
            return
        if parsed.path.startswith("/emby/Users/") and "/Items/" in parsed.path:
            self._send(self.state.item())
            return
        if parsed.path == "/emby/Users":
            self._send([{"Id": "user-1", "Name": "admin"}])
            return
        self._send({"message": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
        """处理 PlaybackInfo 与刷新请求。"""
        parsed = urlparse(self.path)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.state.requests.append(("POST", parsed.path, query))
        if query.get("api_key") != "test-key":
            self._send({"message": "Requires authentication"}, 401)
            return
        if parsed.path.endswith("/PlaybackInfo"):
            if self.state.fail_playbackinfo > 0:
                self.state.fail_playbackinfo -= 1
                self._send({"message": "boom"}, 500)
                return
            # 模拟 Emby 完成真实探测后把媒体信息写入库
            self.state.probed = True
            self._send({"MediaSources": [{"Id": "src", "Container": "mp4"}], "Body": body.decode() or "{}"})
            return
        if parsed.path.endswith("/Refresh") or parsed.path == "/emby/Library/Refresh":
            self._send(None, 204)
            return
        self._send({"message": "not found"}, 404)


class RealRequestUtils:
    """用 requests 实现的最小 RequestUtils，签名与宿主一致。"""

    def __init__(self, timeout: int = None, content_type: str = None, **kwargs) -> None:
        self._timeout = timeout or 20
        self._headers = {"Content-Type": content_type} if content_type else {}

    def get_res(self, url: str, params: dict = None, **kwargs):
        """发送 GET 请求。"""
        return requests.get(url, params=params, headers=self._headers, timeout=self._timeout)

    def post_res(self, url: str, data=None, params: dict = None, json=None, **kwargs):
        """发送 POST 请求。"""
        return requests.post(url, params=params, json=json, data=data,
                             headers=self._headers, timeout=self._timeout)


@pytest.fixture
def emby(monkeypatch):
    """启动模拟 Emby 服务并把插件的 HTTP 客户端换成真实实现。"""
    state = FakeEmbyState()
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(plugin_module, "RequestUtils", RealRequestUtils)
    host = f"http://127.0.0.1:{server.server_port}"
    instance = types.SimpleNamespace(user="user-1", is_inactive=lambda: False,
                                     _host=host, _apikey="test-key")
    config = types.SimpleNamespace(name="Emby", type="emby",
                                   config={"host": host, "apikey": "test-key"})
    service = types.SimpleNamespace(name="Emby", type="emby", instance=instance, config=config)
    try:
        yield state, service
    finally:
        server.shutdown()
        server.server_close()


def _plugin(**config):
    """构造插件实例（不启动后台线程）。"""
    instance = plugin_module.StrmPrewarmer()
    base = {"enabled": False, "retry_interval": 0}
    base.update(config)
    instance.init_plugin(base)
    return instance


def test_find_item_by_path_uses_path_query(emby):
    """路径查询命中时应只发出一次 Items 请求。"""
    state, service = emby
    plugin = _plugin()
    item = plugin._find_item_by_path(service, state.item_path)
    assert item["Id"] == "1001"
    gets = [entry for entry in state.requests if entry[0] == "GET" and entry[1] == "/emby/Items"]
    assert len(gets) == 1
    assert gets[0][2]["Path"] == state.item_path
    assert gets[0][2]["api_key"] == "test-key"
    assert gets[0][2]["Recursive"] == "true"


def test_find_item_falls_back_to_deep_scan_over_http(emby):
    """老版本 Emby 不支持 Path 查询时，深度遍历仍能找到条目。"""
    state, service = emby
    state.support_path_query = False
    plugin = _plugin()
    item = plugin._find_item_by_path(service, state.item_path)
    assert item["Id"] == "1001"
    assert any(entry[2].get("StartIndex") == "0" for entry in state.requests if entry[0] == "GET")


def test_prewarm_completes_mediainfo_over_http(emby):
    """PlaybackInfo 请求后条目媒体信息应变为完整。"""
    state, service = emby
    plugin = _plugin()
    assert plugin_module.has_complete_mediainfo(plugin._fetch_item(service, "1001")) is False
    ok, summary = plugin._prewarm(service, "1001")
    assert ok is True
    assert "3840x2160" in summary and "HEVC" in summary
    posts = [entry for entry in state.requests if entry[0] == "POST"]
    assert posts[0][1] == "/emby/Items/1001/PlaybackInfo"
    assert posts[0][2]["IsPlayback"] == "true"
    assert posts[0][2]["UserId"] == "user-1"
    assert posts[0][2]["MaxStreamingBitrate"] == "200000000"


def test_process_target_end_to_end(emby):
    """从定位条目到写入历史的完整链路应返回成功记录。"""
    state, service = emby
    plugin = _plugin()
    item = plugin._find_item_by_path(service, state.item_path)
    record = plugin._process_target("Emby", service, item, "Demo", "入库")
    assert record["status"] == "success"
    assert "3840x2160" in record["detail"]
    assert record["item_id"] == "1001"


def test_process_target_retries_over_http(emby):
    """PlaybackInfo 前两次失败时应重试并最终成功。"""
    state, service = emby
    state.fail_playbackinfo = 2
    plugin = _plugin(max_retries=2, retry_interval=0)
    item = plugin._find_item_by_path(service, state.item_path)
    record = plugin._process_target("Emby", service, item, "Demo", "入库")
    assert record["status"] == "success"
    playback_posts = [entry for entry in state.requests
                      if entry[0] == "POST" and entry[1].endswith("/PlaybackInfo")]
    assert len(playback_posts) == 3


def test_wrong_apikey_is_reported(emby):
    """密钥错误时应返回 HTTP 401 描述而不是抛异常。"""
    _state, service = emby
    service.config.config["apikey"] = "bad-key"
    service.instance._apikey = "bad-key"
    plugin = _plugin()
    ok, message = plugin._request(service, "GET", "Items", {"Limit": "1"})
    assert ok is False
    assert "401" in message


def test_missing_endpoint_is_reported(emby):
    """缺少地址或密钥时应返回明确错误。"""
    _state, service = emby
    service.config.config.update({"host": "", "apikey": ""})
    service.instance._host = ""
    service.instance._apikey = ""
    plugin = _plugin()
    ok, message = plugin._request(service, "GET", "Items")
    assert ok is False
    assert "缺失" in message
