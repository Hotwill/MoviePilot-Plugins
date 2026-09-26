"""STRM 媒体信息预热插件单元测试。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin_loader import STUBS, load_plugin  # noqa: E402

plugin_module = load_plugin("strmprewarmer")


def test_normalize_base_url_adds_scheme_and_slash():
    """地址标准化应补全协议和结尾斜杠。"""
    assert plugin_module.normalize_base_url("192.168.1.2:8096") == "http://192.168.1.2:8096/"
    assert plugin_module.normalize_base_url("https://emby.test/") == "https://emby.test/"
    assert plugin_module.normalize_base_url("") == ""


@pytest.mark.parametrize("line,expected", [
    ("/media => /data/media", ("/media", "/data/media")),
    ("/media|/data/media", ("/media", "/data/media")),
    ("/media:/data/media", ("/media", "/data/media")),
    ("C:\\media => /data/media", ("C:\\media", "/data/media")),
    ("# comment", None),
    ("", None),
])
def test_split_mapping_line(line, expected):
    """映射行解析应支持多种分隔符并忽略注释。"""
    assert plugin_module.split_mapping_line(line) == expected


def test_parse_mappings_sorted_by_prefix_length():
    """映射表应按前缀长度降序，保证最长前缀优先。"""
    mappings = plugin_module.parse_mappings("/a => /x\n/a/b => /y")
    assert mappings[0][0] == "/a/b"


def test_apply_mapping_forward_and_reverse():
    """正向映射转换为 Emby 路径，反向映射还原本地路径。"""
    mappings = plugin_module.parse_mappings("/media/strm => /data/media")
    emby_path = plugin_module.apply_mapping("/media/strm/movies/a.strm", mappings)
    assert emby_path == "/data/media/movies/a.strm"
    assert plugin_module.apply_mapping(emby_path, mappings, reverse=True) == "/media/strm/movies/a.strm"


def test_apply_mapping_without_match_returns_input():
    """没有匹配前缀时返回原路径。"""
    mappings = plugin_module.parse_mappings("/media => /data")
    assert plugin_module.apply_mapping("/other/a.strm", mappings) == "/other/a.strm"


def test_is_strm():
    """STRM 判定应忽略大小写。"""
    assert plugin_module.is_strm("/a/b.STRM")
    assert not plugin_module.is_strm("/a/b.mkv")
    assert not plugin_module.is_strm("")


def test_has_complete_mediainfo_requires_codec_and_size():
    """只有同时具备编码和分辨率才算媒体信息完整。"""
    complete = {"MediaSources": [{"MediaStreams": [
        {"Type": "Video", "Codec": "hevc", "Width": 1920, "Height": 1080}]}]}
    incomplete = {"MediaSources": [{"MediaStreams": [{"Type": "Video", "Codec": "hevc"}]}]}
    assert plugin_module.has_complete_mediainfo(complete)
    assert not plugin_module.has_complete_mediainfo(incomplete)
    assert not plugin_module.has_complete_mediainfo({})
    assert not plugin_module.has_complete_mediainfo(None)


def test_has_complete_mediainfo_supports_flat_streams():
    """兼容直接返回 MediaStreams 的条目结构。"""
    item = {"MediaStreams": [{"Type": "Video", "Codec": "h264", "Width": 1280, "Height": 720}]}
    assert plugin_module.has_complete_mediainfo(item)


def test_describe_mediainfo_summary():
    """媒体信息摘要应包含分辨率、编码、码率和音轨数量。"""
    item = {"MediaSources": [{"MediaStreams": [
        {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "BitRate": 25000000},
        {"Type": "Audio", "Codec": "eac3"},
        {"Type": "Audio", "Codec": "aac"},
    ]}]}
    summary = plugin_module.describe_mediainfo(item)
    assert "3840x2160" in summary
    assert "HEVC" in summary
    assert "25.0Mbps" in summary
    assert "2音轨" in summary


def test_file_fingerprint(tmp_path):
    """指纹应包含大小与哈希，文件缺失时返回不可用。"""
    strm = tmp_path / "a.strm"
    strm.write_text("http://host/redirect?path=/a.mkv", encoding="utf-8")
    fingerprint = plugin_module.file_fingerprint(str(strm))
    assert fingerprint["status"] == "ok"
    assert fingerprint["size"] == len("http://host/redirect?path=/a.mkv")
    assert len(fingerprint["sha256"]) == 64
    assert plugin_module.file_fingerprint(str(tmp_path / "missing.strm"))["status"] == "unavailable"
    assert plugin_module.file_fingerprint(None)["status"] == "unavailable"


def test_parse_roots_filters_comments():
    """根目录解析应过滤空行与注释。"""
    assert plugin_module.parse_roots("/a\n\n# c\n /b ") == ("/a", "/b")


def _service(host="http://emby:8096", apikey="key", user="u1"):
    """构造一个假的媒体服务器服务对象。"""
    instance = types.SimpleNamespace(user=user, is_inactive=lambda: False, _host=host, _apikey=apikey)
    config = types.SimpleNamespace(name="Emby", type="emby", config={"host": host, "apikey": apikey})
    return types.SimpleNamespace(name="Emby", type="emby", instance=instance, config=config)


def _plugin(**config):
    """构造已初始化但未启动后台线程的插件实例。"""
    instance = plugin_module.StrmPrewarmer()
    base = {"enabled": False}
    base.update(config)
    instance.init_plugin(base)
    return instance


def test_endpoint_reads_config_first():
    """端点解析优先使用服务配置中的地址与密钥。"""
    endpoint = plugin_module.StrmPrewarmer._endpoint(_service())
    assert endpoint == ("http://emby:8096/", "key", "u1")


def test_endpoint_missing_returns_none():
    """缺少地址或密钥时返回 None。"""
    service = _service(host="", apikey="")
    assert plugin_module.StrmPrewarmer._endpoint(service) is None


def test_request_builds_emby_url_with_api_key():
    """请求应拼接 emby 前缀并带上 api_key。"""
    STUBS.request_utils.calls.clear()
    STUBS.request_utils.responses = {"default": STUBS.response(200, {"Items": []}, b"{}")}
    instance = _plugin()
    ok, data = instance._request(_service(), "GET", "Items", {"Limit": "1"})
    assert ok is True
    assert data == {"Items": []}
    method, url, params = STUBS.request_utils.calls[0]
    assert method == "GET"
    assert url == "http://emby:8096/emby/Items"
    assert params["api_key"] == "key"
    assert params["Limit"] == "1"


def test_request_reports_http_error():
    """非 200/204 响应应返回失败与状态码描述。"""
    STUBS.request_utils.calls.clear()
    STUBS.request_utils.responses = {"default": STUBS.response(401, None, b"")}
    instance = _plugin()
    ok, message = instance._request(_service(), "GET", "Items")
    assert ok is False
    assert "401" in message


def test_find_item_by_path_matches_exact_path():
    """路径查询应返回路径完全匹配的条目。"""
    instance = _plugin()
    instance._query_items = lambda service, params: [
        {"Id": "1", "Path": "/data/media/other.strm"},
        {"Id": "2", "Path": "/data/media/target.strm"},
    ]
    item = instance._find_item_by_path(_service(), "/data/media/target.strm")
    assert item["Id"] == "2"


def test_find_item_by_path_returns_none_when_no_match():
    """没有匹配条目时返回 None。"""
    instance = _plugin()
    instance._query_items = lambda service, params: []
    assert instance._find_item_by_path(_service(), "/data/media/target.strm") is None


def test_prewarm_success_returns_summary():
    """预热成功后应返回媒体信息摘要。"""
    instance = _plugin()
    instance._request = lambda *args, **kwargs: (True, None)
    instance._fetch_item = lambda service, item_id: {"MediaSources": [{"MediaStreams": [
        {"Type": "Video", "Codec": "hevc", "Width": 1920, "Height": 1080}]}]}
    ok, message = instance._prewarm(_service(), "1")
    assert ok is True
    assert "1920x1080" in message


def test_prewarm_detects_incomplete_result():
    """PlaybackInfo 成功但媒体信息仍缺失时应判定失败。"""
    instance = _plugin()
    instance._request = lambda *args, **kwargs: (True, None)
    instance._fetch_item = lambda service, item_id: {"MediaSources": [{"MediaStreams": []}]}
    ok, message = instance._prewarm(_service(), "1")
    assert ok is False
    assert "不完整" in message


def test_process_target_skips_complete_item():
    """已有完整媒体信息且源未变化时应跳过预热。"""
    instance = _plugin()
    called = []
    instance._prewarm = lambda service, item_id: called.append(item_id) or (True, "")
    item = {"Id": "9", "Path": "/data/media/a.strm", "Name": "A",
            "MediaSources": [{"MediaStreams": [
                {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}]}]}
    record = instance._process_target("Emby", _service(), item, "A", "入库")
    assert record["status"] == "skip"
    assert not called


def test_process_target_prewarms_incomplete_item():
    """媒体信息缺失时应执行预热并记录成功。"""
    instance = _plugin()
    instance._prewarm = lambda service, item_id: (True, "1920x1080 H264")
    item = {"Id": "10", "Path": "/data/media/b.strm", "Name": "B", "MediaSources": [{"MediaStreams": []}]}
    record = instance._process_target("Emby", _service(), item, "B", "入库")
    assert record["status"] == "success"
    assert "1920x1080" in record["detail"]


def test_process_target_retries_then_fails():
    """预热持续失败时应按配置重试并记录失败。"""
    instance = _plugin(max_retries=2, retry_interval=0)
    attempts = []

    def failing(service, item_id):
        """始终失败的预热实现。"""
        attempts.append(item_id)
        return False, "网络错误"

    instance._prewarm = failing
    item = {"Id": "11", "Path": "/data/media/c.strm", "Name": "C", "MediaSources": [{"MediaStreams": []}]}
    record = instance._process_target("Emby", _service(), item, "C", "入库")
    assert record["status"] == "fail"
    assert len(attempts) == 3
    assert record["detail"] == "网络错误"


def test_process_target_detects_source_change(tmp_path):
    """同名 STRM 换源后应重新刷新并预热。"""
    strm = tmp_path / "d.strm"
    strm.write_text("new-link", encoding="utf-8")
    instance = _plugin(path_mappings=f"{tmp_path} => /data/media")
    instance.save_data("fingerprints", {"12": {"path": "/data/media/d.strm",
                                              "signature": {"status": "ok", "size": 1, "sha256": "old"}}})
    refreshed, prewarmed = [], []
    instance._trigger_refresh = lambda service, item_id=None: refreshed.append(item_id)
    instance._prewarm = lambda service, item_id: prewarmed.append(item_id) or (True, "1920x1080")
    item = {"Id": "12", "Path": "/data/media/d.strm", "Name": "D",
            "MediaSources": [{"MediaStreams": [
                {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}]}]}
    record = instance._process_target("Emby", _service(), item, "D", "定时")
    assert refreshed == ["12"]
    assert prewarmed == ["12"]
    assert record["status"] == "changed"
    assert record["detail"].startswith("换源重新预热")
    assert instance.get_data("fingerprints")["12"]["signature"]["sha256"] != "old"


def test_collect_scan_targets_filters_roots_and_extension():
    """全量扫描应只保留配置目录下缺信息的 STRM 条目。"""
    instance = _plugin(scan_roots="/data/media/movies")
    pages = [[
        {"Id": "1", "Path": "/data/media/movies/a.strm", "MediaSources": [{"MediaStreams": []}]},
        {"Id": "2", "Path": "/data/media/tv/b.strm", "MediaSources": [{"MediaStreams": []}]},
        {"Id": "3", "Path": "/data/media/movies/c.mkv", "MediaSources": [{"MediaStreams": []}]},
    ]]
    instance._query_items = lambda service, params: pages.pop(0) if pages else []
    targets = instance._collect_scan_targets(_service(), "Emby")
    assert [(item["Id"], reason) for item, reason in targets] == [("1", "incomplete")]


def test_collect_scan_targets_respects_max_items():
    """单次处理上限应生效。"""
    instance = _plugin(max_items=2)
    pages = [[{"Id": str(i), "Path": f"/data/{i}.strm", "MediaSources": [{"MediaStreams": []}]}
              for i in range(5)]]
    instance._query_items = lambda service, params: pages.pop(0) if pages else []
    targets = instance._collect_scan_targets(_service(), "Emby")
    assert len(targets) == 2


def test_record_history_keeps_limit():
    """历史记录应按上限截断且最新在前。"""
    instance = _plugin(history_count=20)
    instance._record_history([{"time": "t1", "title": "old"}])
    instance._record_history([{"time": "t2", "title": "new"}])
    history = instance.get_data("history")
    assert history[0]["title"] == "new"
    assert len(history) == 2


def test_notify_records_only_on_failure_by_default():
    """默认只在失败时推送通知。"""
    instance = _plugin(notify=True)
    instance._notify_records([{"status": "success", "title": "A", "detail": ""}])
    assert instance.messages == []
    instance._notify_records([{"status": "fail", "title": "B", "detail": "错误"}])
    assert len(instance.messages) == 1
    assert "失败" in instance.messages[0]["title"]


def test_notify_success_switch():
    """开启成功通知后应推送成功结果。"""
    instance = _plugin(notify=True, notify_success=True)
    instance._notify_records([{"status": "success", "title": "A", "detail": "1920x1080"}])
    assert len(instance.messages) == 1


def test_enqueue_paths_filters_non_strm():
    """仅处理 STRM 时应过滤其它文件。"""
    instance = _plugin()
    instance._enqueue_paths(["/a/b.mkv", "/a/c.strm"], "T", "入库")
    assert instance._queue.qsize() == 1
    task = instance._queue.get_nowait()
    assert task["path"] == "/a/c.strm"


def test_enqueue_paths_allows_all_when_only_strm_disabled():
    """关闭仅 STRM 后所有文件都会入队。"""
    instance = _plugin(only_strm=False)
    instance._enqueue_paths(["/a/b.mkv", "/a/c.strm"], "T", "入库")
    assert instance._queue.qsize() == 2


def test_get_service_requires_enabled_and_cron():
    """只有启用且配置周期时才注册定时服务。"""
    assert _plugin().get_service() == []
    instance = _plugin(enabled=True, cron="0 3 * * *")
    try:
        services = instance.get_service()
        assert services[0]["id"] == "StrmPrewarmer.FullScan"
    finally:
        instance.stop_service()


def test_get_service_rejects_invalid_cron():
    """非法周期表达式不应注册服务。"""
    instance = _plugin(enabled=True, cron="not a cron")
    try:
        assert instance.get_service() == []
    finally:
        instance.stop_service()


def test_get_form_defaults_contain_all_models():
    """配置表单默认值应覆盖所有表单字段。"""
    instance = _plugin()
    form, defaults = instance.get_form()
    models = set()

    def walk(node):
        """递归收集表单中的 model 字段。"""
        if isinstance(node, dict):
            model = (node.get("props") or {}).get("model")
            if model:
                models.add(model)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(form)
    assert models
    assert models.issubset(set(defaults))


def test_get_page_without_history():
    """无历史时详情页展示提示信息。"""
    instance = _plugin()
    page = instance.get_page()
    assert page[0]["component"] == "VAlert"


def test_get_page_renders_history_rows():
    """有历史时详情页渲染表格行。"""
    instance = _plugin()
    instance.save_data("history", [{"time": "t", "source": "入库", "title": "A",
                                    "status": "success", "detail": "1920x1080", "path": "/a.strm"}])
    page = instance.get_page()
    assert page[0]["component"] == "VTable"
    body = page[0]["content"][1]
    assert len(body["content"]) == 1


def test_events_registered():
    """插件应注册入库、Webhook 和命令事件。"""
    names = {name for _, name in STUBS.event_manager.registered}
    assert {"on_transfer_complete", "on_webhook_message", "on_plugin_action"}.issubset(names)


def test_transfer_event_enqueues_strm_path():
    """入库事件应把 STRM 目标路径加入队列。"""
    instance = _plugin(enabled=False)
    instance._enabled = True
    instance._listen_transfer = True
    transferinfo = types.SimpleNamespace(
        target_item=types.SimpleNamespace(path="/media/strm/a.strm"),
        file_list_new=["/media/strm/a.strm"])
    mediainfo = types.SimpleNamespace(title_year="电影 (2024)", title="电影")
    event = types.SimpleNamespace(event_data={"transferinfo": transferinfo, "mediainfo": mediainfo})
    instance.on_transfer_complete(event)
    assert instance._queue.qsize() == 1
    task = instance._queue.get_nowait()
    assert task["path"] == "/media/strm/a.strm"
    assert task["title"] == "电影 (2024)"


def test_transfer_event_ignored_when_disabled():
    """未启用监听时不处理入库事件。"""
    instance = _plugin()
    instance._enabled = True
    instance._listen_transfer = False
    transferinfo = types.SimpleNamespace(
        target_item=types.SimpleNamespace(path="/media/strm/a.strm"), file_list_new=[])
    event = types.SimpleNamespace(event_data={"transferinfo": transferinfo, "mediainfo": None})
    instance.on_transfer_complete(event)
    assert instance._queue.qsize() == 0


def test_webhook_event_enqueues_item():
    """Webhook 新增入库事件应按条目 ID 入队。"""
    instance = _plugin()
    instance._enabled = True
    event_info = types.SimpleNamespace(channel="emby", event="library.new", item_id="55",
                                       item_path="/data/media/a.strm", item_name="A", server_name="Emby")
    instance.on_webhook_message(types.SimpleNamespace(event_data=event_info))
    task = instance._queue.get_nowait()
    assert task["item_id"] == "55"
    assert task["source"] == "Webhook"


def test_webhook_event_ignores_other_events():
    """非新增入库的 Webhook 事件应忽略。"""
    instance = _plugin()
    instance._enabled = True
    event_info = types.SimpleNamespace(channel="emby", event="playback.start", item_id="55",
                                       item_path="/data/media/a.strm", item_name="A", server_name="Emby")
    instance.on_webhook_message(types.SimpleNamespace(event_data=event_info))
    assert instance._queue.qsize() == 0


def test_plugin_action_triggers_scan():
    """插件动作事件应触发全量扫描任务。"""
    instance = _plugin()
    instance._enabled = True
    instance.on_plugin_action(types.SimpleNamespace(event_data={"action": "strm_prewarm"}))
    assert instance._queue.get_nowait()["type"] == "scan"


def test_stop_service_is_idempotent():
    """停止服务可以重复调用。"""
    instance = _plugin(enabled=True)
    instance.stop_service()
    instance.stop_service()
    assert instance._workers == []


def test_command_declaration():
    """远程命令声明应包含动作数据。"""
    command = plugin_module.StrmPrewarmer.get_command()[0]
    assert command["cmd"] == "/strm_prewarm"
    assert command["data"] == {"action": "strm_prewarm"}


def test_is_playable_rejects_series():
    """剧集/季条目不能直接执行媒体探测。"""
    assert plugin_module.is_playable({"Type": "Episode", "Path": "/a/b.strm"})
    assert plugin_module.is_playable({"Type": "Movie", "Path": "/a/b.strm"})
    assert not plugin_module.is_playable({"Type": "Series", "Path": "/a"})
    assert not plugin_module.is_playable({"Type": "Season", "Path": "/a"})
    assert not plugin_module.is_playable({"Type": "Movie"})
    assert not plugin_module.is_playable(None)


def test_locate_item_prefers_path_over_series_id():
    """Webhook 剧集事件给的是剧集 ID，应按路径定位到分集。"""
    instance = _plugin()
    episode = {"Id": "ep1", "Type": "Episode", "Path": "/data/media/tv/s01e01.strm"}
    series = {"Id": "series1", "Type": "Series", "Path": "/data/media/tv"}
    instance._find_item_by_path = lambda service, path: episode if path == episode["Path"] else None
    instance._fetch_item = lambda service, item_id: series
    task = {"item_id": "series1", "path": episode["Path"], "path_side": "emby"}
    assert instance._locate_item(_service(), task, "剧集 S01E01")["Id"] == "ep1"


def test_locate_item_rejects_series_when_path_missing():
    """路径不可用且条目是剧集时不应返回条目。"""
    instance = _plugin()
    instance._fetch_item = lambda service, item_id: {"Id": "series1", "Type": "Series", "Path": "/data/tv"}
    assert instance._locate_item(_service(), {"item_id": "series1"}, "剧集") is None


def test_locate_item_accepts_movie_by_id():
    """电影 Webhook 给的是条目本身，允许按 ID 定位。"""
    instance = _plugin()
    movie = {"Id": "m1", "Type": "Movie", "Path": "/data/media/movies/a.strm"}
    instance._fetch_item = lambda service, item_id: movie
    assert instance._locate_item(_service(), {"item_id": "m1"}, "电影")["Id"] == "m1"


def test_locate_item_maps_local_path_for_transfer_tasks():
    """入库任务的路径需要按映射转换成 Emby 路径。"""
    instance = _plugin(path_mappings="/media/strm => /data/media")
    seen = []

    def wait(service, path, title):
        """记录查找时使用的路径。"""
        seen.append(path)
        return {"Id": "1", "Type": "Movie", "Path": path}

    instance._wait_for_item = wait
    instance._locate_item(_service(), {"path": "/media/strm/a.strm", "path_side": "local"}, "A")
    assert seen == ["/data/media/a.strm"]


def test_find_item_by_path_falls_back_to_deep_scan():
    """Path 查询与搜索都失败时使用深度遍历兜底。"""
    instance = _plugin()
    target = {"Id": "7", "Path": "/data/media/deep.strm"}
    calls = []

    def query(service, params):
        """模拟 Path/SearchTerm 查询为空，分页遍历命中。"""
        calls.append(params)
        if "StartIndex" in params:
            return [target] if params["StartIndex"] == "0" else []
        return []

    instance._query_items = query
    instance._fetch_item = lambda service, item_id: dict(target, Type="Movie")
    found = instance._find_item_by_path(_service(), "/data/media/deep.strm")
    assert found["Id"] == "7"
    assert any("StartIndex" in params for params in calls)


def test_find_item_by_path_deep_scan_can_be_disabled():
    """关闭深度查找后不应执行分页遍历。"""
    instance = _plugin(deep_lookup=False)
    calls = []

    def query(service, params):
        """记录查询参数。"""
        calls.append(params)
        return []

    instance._query_items = query
    assert instance._find_item_by_path(_service(), "/data/media/deep.strm") is None
    assert not any("StartIndex" in params for params in calls)


def test_webhook_task_marks_emby_path_and_zero_delay():
    """Webhook 任务应标记 Emby 侧路径且不再延迟等待。"""
    instance = _plugin()
    instance._enabled = True
    event_info = types.SimpleNamespace(channel="emby", event="library.new", item_id="55",
                                       item_path="/data/media/a.strm", item_name="A", server_name="Emby")
    instance.on_webhook_message(types.SimpleNamespace(event_data=event_info))
    task = instance._queue.get_nowait()
    assert task["path_side"] == "emby"
    assert task["delay"] == 0


def test_transfer_task_marks_local_path():
    """入库任务应标记 MoviePilot 侧路径。"""
    instance = _plugin()
    instance._enabled = True
    transferinfo = types.SimpleNamespace(
        target_item=types.SimpleNamespace(path="/media/strm/a.strm"), file_list_new=[])
    event = types.SimpleNamespace(event_data={"transferinfo": transferinfo, "mediainfo": None})
    instance.on_transfer_complete(event)
    assert instance._queue.get_nowait()["path_side"] == "local"


def test_locate_item_emby_path_does_not_poll():
    """Webhook 路径已在库中，应直接查询而不进入轮询等待。"""
    instance = _plugin()
    polled = []
    instance._wait_for_item = lambda service, path, title: polled.append(path)
    instance._find_item_by_path = lambda service, path: {"Id": "1", "Type": "Movie", "Path": path}
    item = instance._locate_item(_service(), {"path": "/data/media/a.strm", "path_side": "emby"}, "A")
    assert item["Id"] == "1"
    assert polled == []


def test_dedup_window_blocks_repeat():
    """去重窗口内同一条目不应重复处理。"""
    instance = _plugin(dedup_window=600)
    key = ("Emby", "1")
    assert instance._recently_done(key) is False
    instance._mark_done(key)
    assert instance._recently_done(key) is True


def test_dedup_window_disabled():
    """去重窗口为 0 时不做去重。"""
    instance = _plugin(dedup_window=0)
    key = ("Emby", "1")
    instance._mark_done(key)
    assert instance._recently_done(key) is False


def test_handle_task_skips_duplicate_trigger():
    """入库事件与 Webhook 重复触发同一条目时只处理一次。"""
    instance = _plugin(dedup_window=600, delay=0)
    item = {"Id": "42", "Type": "Movie", "Path": "/data/media/a.strm", "MediaSources": [{"MediaStreams": []}]}
    processed = []
    service = _service()
    instance.__class__.service_infos = property(lambda self: {"Emby": service})
    try:
        instance._locate_item = lambda svc, task, title: item
        instance._process_target = lambda name, svc, it, title, source, reason=None, image="": (
            processed.append(it["Id"]) or {"status": "success", "title": title, "detail": ""})
        instance._handle_task({"path": "/data/media/a.strm", "path_side": "emby", "source": "入库"})
        instance._handle_task({"item_id": "42", "source": "Webhook"})
    finally:
        del instance.__class__.service_infos
    assert processed == ["42"]


def test_notify_treats_changed_as_success():
    """换源重新预热成功时按成功通知。"""
    instance = _plugin(notify=True, notify_success=True)
    instance._notify_records([{"status": "changed", "title": "A", "detail": "换源重新预热 1920x1080"}])
    assert len(instance.messages) == 1
    assert "🔄" in instance.messages[0]["text"]


def test_collect_scan_targets_marks_changed_reason(tmp_path):
    """指纹变化的条目应标记为 changed。"""
    strm = tmp_path / "e.strm"
    strm.write_text("new", encoding="utf-8")
    instance = _plugin(path_mappings=f"{tmp_path} => /data/media")
    instance.save_data("fingerprints", {"5": {"path": "/data/media/e.strm",
                                             "signature": {"status": "ok", "size": 1, "sha256": "old"}}})
    pages = [[{"Id": "5", "Path": "/data/media/e.strm", "MediaSources": [{"MediaStreams": [
        {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}]}]}]]
    instance._query_items = lambda service, params: pages.pop(0) if pages else []
    targets = instance._collect_scan_targets(_service(), "Emby")
    assert [reason for _, reason in targets] == ["changed"]


def test_process_target_changed_reason_refreshes_first():
    """调用方传入 changed 时应先刷新条目再预热。"""
    instance = _plugin()
    order = []
    instance._trigger_refresh = lambda service, item_id=None: order.append(("refresh", item_id))
    instance._prewarm = lambda service, item_id: (order.append(("prewarm", item_id)), (True, "1920x1080"))[1]
    item = {"Id": "5", "Path": "/data/media/e.strm", "Name": "E",
            "MediaSources": [{"MediaStreams": [
                {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}]}]}
    record = instance._process_target("Emby", _service(), item, "E", "定时", reason="changed")
    assert order == [("refresh", "5"), ("prewarm", "5")]
    assert record["status"] == "changed"


def test_process_target_incomplete_reason_skips_check():
    """调用方传入 incomplete 时直接预热，不再判断完整性。"""
    instance = _plugin()
    calls = []
    instance._prewarm = lambda service, item_id: (calls.append(item_id), (True, "1920x1080"))[1]
    complete_item = {"Id": "6", "Path": "/data/media/f.strm", "Name": "F",
                     "MediaSources": [{"MediaStreams": [
                         {"Type": "Video", "Codec": "h264", "Width": 1920, "Height": 1080}]}]}
    record = instance._process_target("Emby", _service(), complete_item, "F", "定时", reason="incomplete")
    assert calls == ["6"]
    assert record["status"] == "success"


def test_get_api_declares_three_endpoints():
    """插件应注册状态、历史与外部触发三个接口。"""
    instance = _plugin()
    apis = instance.get_api()
    assert [api["path"] for api in apis] == ["/status", "/history", "/prewarm"]
    assert [api["methods"] for api in apis] == [["GET"], ["GET"], ["POST"]]
    assert [api["auth"] for api in apis] == ["bear", "bear", "apikey"]
    for api in apis:
        assert callable(api["endpoint"])
        assert api["response_model"] is plugin_module.ApiResult


def test_api_status_reports_runtime_state():
    """状态接口应返回启用状态、队列长度与历史统计。"""
    instance = _plugin()
    instance._enabled = True
    instance.save_data("history", [
        {"status": "success", "title": "A"},
        {"status": "fail", "title": "B"},
        {"status": "success", "title": "C"},
    ])
    result = instance.api_status()
    assert result.success is True
    assert result.data["enabled"] is True
    assert result.data["queued"] == 0
    assert result.data["history_counts"] == {"success": 2, "fail": 1}
    assert result.data["last_record"]["title"] == "A"


def test_api_history_respects_limit():
    """历史接口应按 limit 截断并返回总数。"""
    instance = _plugin()
    instance.save_data("history", [{"status": "success", "title": str(i)} for i in range(10)])
    result = instance.api_history(limit=3)
    assert result.data["total"] == 10
    assert len(result.data["records"]) == 3


def test_api_prewarm_enqueues_task():
    """外部触发接口应把任务加入队列。"""
    instance = _plugin()
    instance._enabled = True
    result = instance.api_prewarm(path="/media/strm/a.strm")
    assert result.success is True
    task = instance._queue.get_nowait()
    assert task["path"] == "/media/strm/a.strm"
    assert task["path_side"] == "local"
    assert task["source"] == "API"


def test_api_prewarm_accepts_emby_side_and_item_id():
    """接口支持 Emby 侧路径与条目 ID。"""
    instance = _plugin()
    instance._enabled = True
    assert instance.api_prewarm(item_id="99", side="emby").success is True
    task = instance._queue.get_nowait()
    assert task["item_id"] == "99"
    assert task["type"] == "item"
    assert task["path_side"] == "emby"


def test_api_prewarm_validates_input():
    """接口应校验启用状态、参数与文件类型。"""
    disabled = _plugin()
    assert disabled.api_prewarm(path="/a.strm").success is False

    instance = _plugin()
    instance._enabled = True
    assert instance.api_prewarm().success is False
    assert "path" in instance.api_prewarm().message
    assert instance.api_prewarm(path="/a.mkv").success is False
    assert instance.api_prewarm(path="/a.strm", side="other").success is False
    assert instance._queue.qsize() == 0


def test_api_prewarm_allows_non_strm_when_configured():
    """关闭仅 STRM 限制后非 STRM 也能触发。"""
    instance = _plugin(only_strm=False)
    instance._enabled = True
    assert instance.api_prewarm(path="/a.mkv").success is True


def test_describe_streams_fields():
    """媒体流应拆成分辨率、编码、码率、音轨、字幕等字段。"""
    item = {"MediaSources": [{"MediaStreams": [
        {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160,
         "BitRate": 25000000, "VideoRange": "HDR"},
        {"Type": "Audio", "Codec": "eac3"},
        {"Type": "Audio", "Codec": "aac"},
        {"Type": "Subtitle", "Codec": "subrip"},
    ]}]}
    fields = plugin_module.describe_streams(item)
    assert fields["resolution"] == "3840x2160"
    assert fields["codec"] == "HEVC"
    assert fields["bitrate"] == "25.0Mbps"
    assert fields["range"] == "HDR"
    assert fields["audio"] == "AAC/EAC3（2条）"
    assert fields["subtitle"] == "1条"


def test_human_elapsed():
    """耗时格式化应区分秒与分钟。"""
    assert plugin_module.human_elapsed(3.14) == "3.1秒"
    assert plugin_module.human_elapsed(75) == "1分15秒"


def test_media_image_prefers_message_image():
    """图片优先取消息图，其次背景图字段。"""
    class Media:
        """带消息图的媒体信息替身。"""

        def get_message_image(self):
            """返回消息图。"""
            return "http://img/message.jpg"

    assert plugin_module.media_image(Media()) == "http://img/message.jpg"
    assert plugin_module.media_image(types.SimpleNamespace(backdrop_path="http://img/b.jpg")) == \
        "http://img/b.jpg"
    assert plugin_module.media_image(None) == ""


def test_single_record_notification_has_image_and_fields():
    """单条通知应带图片，并按行展示画面、编码、音轨等信息。"""
    instance = _plugin(notify=True, notify_success=True)
    instance._notify_records([{
        "status": "success", "title": "兰香如故 (2026) S01E01", "server": "Emby",
        "elapsed": 2.5, "image": "http://img/a.jpg", "filename": "a.strm",
        "media": {"resolution": "3840x2160", "range": "HDR", "codec": "HEVC",
                  "bitrate": "25.0Mbps", "audio": "EAC3（1条）"},
    }])
    message = instance.messages[0]
    assert message["image"] == "http://img/a.jpg"
    assert "媒体信息已预热" in message["title"]
    assert "🖼️ 画面：3840x2160 HDR" in message["text"]
    assert "🎞️ 编码：HEVC · 25.0Mbps" in message["text"]
    assert "🔊 音轨：EAC3（1条）" in message["text"]
    assert "⏱️ 耗时：2.5秒" in message["text"]


def test_batch_notification_summarizes():
    """多条通知应给出成功失败汇总与清单。"""
    instance = _plugin(notify=True, notify_success=True)
    instance._notify_records([
        {"status": "success", "title": "A", "media": {"resolution": "1920x1080", "codec": "H264"}},
        {"status": "fail", "title": "B", "detail": "网络错误"},
    ])
    text = instance.messages[0]["text"]
    assert "✅ 成功 1 个" in text and "❌ 失败 1 个" in text
    assert "✅ A（1920x1080 H264）" in text
    assert "❌ B（网络错误）" in text


def test_failure_notification_includes_reason():
    """失败通知应包含失败原因。"""
    instance = _plugin(notify=True)
    instance._notify_records([{"status": "fail", "title": "C", "detail": "PlaybackInfo 超时",
                              "server": "Emby"}])
    assert "⚠️ 原因：PlaybackInfo 超时" in instance.messages[0]["text"]
    assert "预热失败" in instance.messages[0]["title"]
