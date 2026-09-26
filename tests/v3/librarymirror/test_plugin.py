"""媒体库云盘镜像插件单元测试。"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plugin_loader import STUBS, load_plugin  # noqa: E402

plugin_module = load_plugin("librarymirror")


@pytest.fixture(autouse=True)
def clean_storage():
    """每个用例前重置假云盘状态。"""
    STUBS.storage_chain.reset()
    STUBS.directory_helper.dirs = []
    STUBS.storage_helper.storagies = []
    yield
    STUBS.storage_chain.reset()


def _plugin(**config):
    """构造已初始化的插件实例（默认未启用，不启后台线程）。"""
    instance = plugin_module.LibraryMirror()
    base = {"enabled": False, "target_storage": "alist", "target_root": "/cloud/media",
            "retry_interval": 0}
    base.update(config)
    instance.init_plugin(base)
    return instance


# ----------------------------------------------------------------- 纯函数


def test_normalize_remote_path():
    """云盘根目录应补全前导斜杠并去掉尾部斜杠。"""
    assert plugin_module.normalize_remote_path("cloud/media/") == "/cloud/media"
    assert plugin_module.normalize_remote_path("/cloud/media") == "/cloud/media"
    assert plugin_module.normalize_remote_path("  ") == ""
    assert plugin_module.normalize_remote_path("/") == "/"


def test_relative_to_root_keeps_library_structure():
    """相对路径应保留「类型/分类/剧名 (年份)/季」这层结构。"""
    path = "/media/电视剧/国产剧/兰香如故 (2026)/Season 01/兰香如故 - S01E01.mkv"
    assert plugin_module.relative_to_root(path, ("/media",)) == \
        "电视剧/国产剧/兰香如故 (2026)/Season 01/兰香如故 - S01E01.mkv"


def test_relative_to_root_prefers_longest_root():
    """多个根目录命中时应使用最深的那个。"""
    path = "/media/tv/电视剧/国产剧/剧 (2026)/Season 01/a.mkv"
    assert plugin_module.relative_to_root(path, ("/media", "/media/tv")) == \
        "电视剧/国产剧/剧 (2026)/Season 01/a.mkv"


def test_relative_to_root_returns_none_outside_roots():
    """不在任何根目录下时返回 None。"""
    assert plugin_module.relative_to_root("/other/a.mkv", ("/media",)) is None
    assert plugin_module.relative_to_root("", ("/media",)) is None


def test_parse_extensions_normalizes_and_defaults():
    """扩展名配置应补点、转小写，留空用默认值。"""
    assert plugin_module.parse_extensions("MKV, .mp4\nts", (".x",)) == (".mkv", ".mp4", ".ts")
    assert plugin_module.parse_extensions("", (".mkv",)) == (".mkv",)


def test_is_media_file():
    """只有配置的扩展名才算媒体文件。"""
    assert plugin_module.is_media_file("/a/b.MKV", (".mkv",))
    assert not plugin_module.is_media_file("/a/b.nfo", (".mkv",))
    assert not plugin_module.is_media_file("", (".mkv",))


def test_sidecar_candidates(tmp_path):
    """同名刮削字幕与目录图片都应被识别，媒体文件本身排除。"""
    media = tmp_path / "剧 - S01E01.mkv"
    media.write_bytes(b"v")
    (tmp_path / "剧 - S01E01.nfo").write_bytes(b"n")
    (tmp_path / "剧 - S01E01.zh.srt").write_bytes(b"s")
    (tmp_path / "poster.jpg").write_bytes(b"p")
    (tmp_path / "其它.mkv").write_bytes(b"o")
    found = plugin_module.sidecar_candidates(
        str(media), (".nfo", ".srt"), ("poster.jpg",))
    names = sorted(Path(item).name for item in found)
    assert names == ["poster.jpg", "剧 - S01E01.nfo", "剧 - S01E01.zh.srt"]


def test_sidecar_candidates_without_folder_images(tmp_path):
    """不允许目录图片时 poster.jpg 不应入列。"""
    media = tmp_path / "a.mkv"
    media.write_bytes(b"v")
    (tmp_path / "a.nfo").write_bytes(b"n")
    (tmp_path / "poster.jpg").write_bytes(b"p")
    found = plugin_module.sidecar_candidates(str(media), (".nfo",), ())
    assert [Path(item).name for item in found] == ["a.nfo"]


# ----------------------------------------------------------------- 路径换算


def test_target_path_uses_configured_roots():
    """配置的媒体库根目录应决定云盘路径。"""
    instance = _plugin(source_roots="/media")
    assert instance.target_path("/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv") == \
        "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv"


def test_target_path_falls_back_to_directory_config():
    """未配置根目录时应自动读取 MoviePilot 目录配置。"""
    STUBS.directory_helper.dirs = [types.SimpleNamespace(library_path="/media")]
    instance = _plugin()
    assert instance.library_roots() == ("/media",)
    assert instance.target_path("/media/电影/华语电影/片 (2026)/片.mkv") == \
        "/cloud/media/电影/华语电影/片 (2026)/片.mkv"


def test_target_path_returns_none_outside_roots():
    """路径不在根目录下时返回 None。"""
    instance = _plugin(source_roots="/media")
    assert instance.target_path("/downloads/a.mkv") is None


def test_target_root_slash_only():
    """云盘根目录为 / 时不应产生双斜杠。"""
    instance = _plugin(source_roots="/media", target_root="/")
    assert instance.target_path("/media/电影/a.mkv") == "/电影/a.mkv"


# ----------------------------------------------------------------- 覆盖策略


def test_should_upload_when_remote_missing(tmp_path):
    """云盘不存在时应上传。"""
    local = tmp_path / "a.mkv"
    local.write_bytes(b"12345")
    instance = _plugin()
    needed, reason = instance.should_upload(str(local), None)
    assert needed is True
    assert "不存在" in reason


def test_should_upload_size_strategy(tmp_path):
    """size 策略下大小一致跳过、不一致覆盖。"""
    local = tmp_path / "a.mkv"
    local.write_bytes(b"12345")
    instance = _plugin(overwrite="size")
    same = STUBS.file_item(path="/cloud/media/a.mkv", size=5)
    different = STUBS.file_item(path="/cloud/media/a.mkv", size=9)
    assert instance.should_upload(str(local), same)[0] is False
    assert instance.should_upload(str(local), different)[0] is True


def test_should_upload_never_and_always(tmp_path):
    """never 策略永不覆盖，always 策略总是覆盖。"""
    local = tmp_path / "a.mkv"
    local.write_bytes(b"12345")
    remote = STUBS.file_item(path="/cloud/media/a.mkv", size=5)
    assert _plugin(overwrite="never").should_upload(str(local), remote)[0] is False
    assert _plugin(overwrite="always").should_upload(str(local), remote)[0] is True


# ----------------------------------------------------------------- 镜像流程


def _library(tmp_path):
    """构造一个带季目录结构的本地媒体库。"""
    season = tmp_path / "电视剧" / "国产剧" / "兰香如故 (2026)" / "Season 01"
    season.mkdir(parents=True)
    media = season / "兰香如故 - S01E01.mkv"
    media.write_bytes(b"video-content")
    return media


def test_mirror_file_uploads_with_same_structure(tmp_path):
    """镜像后云盘路径应与媒体库结构一致。"""
    media = _library(tmp_path)
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False)
    item = STUBS.file_item(path=str(media), name=media.name)
    record = instance.mirror_file(item, "兰香如故 (2026)", "入库")
    assert record["status"] == "success"
    expected = "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01/兰香如故 - S01E01.mkv"
    assert record["remote"] == expected
    assert expected in STUBS.storage_chain.remote_files
    folders = [call[2] for call in STUBS.storage_chain.calls if call[0] == "get_folder"]
    assert folders == ["/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01"]


def test_mirror_file_skips_existing_same_size(tmp_path):
    """云盘已有同大小文件时应跳过且不上传。"""
    media = _library(tmp_path)
    target = "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01/兰香如故 - S01E01.mkv"
    STUBS.storage_chain.remote_files[target] = media.stat().st_size
    instance = _plugin(source_roots=str(tmp_path))
    record = instance.mirror_file(STUBS.file_item(path=str(media), name=media.name), "", "入库")
    assert record["status"] == "skip"
    assert not [call for call in STUBS.storage_chain.calls if call[0] == "upload_file"]


def test_mirror_file_copies_sidecars(tmp_path):
    """开启附属文件后 nfo 与字幕也应镜像到同一目录。"""
    media = _library(tmp_path)
    (media.parent / f"{media.stem}.nfo").write_bytes(b"nfo")
    (media.parent / f"{media.stem}.zh.srt").write_bytes(b"srt")
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=True)
    record = instance.mirror_file(STUBS.file_item(path=str(media), name=media.name), "", "入库")
    assert record["status"] == "success"
    assert "附属文件2个" in record["detail"]
    remote = set(STUBS.storage_chain.remote_files)
    base = "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01"
    assert f"{base}/兰香如故 - S01E01.nfo" in remote
    assert f"{base}/兰香如故 - S01E01.zh.srt" in remote


def test_mirror_file_retries_then_succeeds(tmp_path):
    """上传前两次失败时应重试并最终成功。"""
    media = _library(tmp_path)
    STUBS.storage_chain.upload_fails = 2
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False, max_retries=2)
    record = instance.mirror_file(STUBS.file_item(path=str(media), name=media.name), "", "入库")
    assert record["status"] == "success"
    uploads = [call for call in STUBS.storage_chain.calls if call[0] == "upload_file"]
    assert len(uploads) == 3


def test_mirror_file_reports_failure(tmp_path):
    """重试用尽仍失败时记录失败原因。"""
    media = _library(tmp_path)
    STUBS.storage_chain.upload_fails = 99
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False, max_retries=1)
    record = instance.mirror_file(STUBS.file_item(path=str(media), name=media.name), "", "入库")
    assert record["status"] == "fail"
    assert record["detail"] == "上传失败"


def test_mirror_file_reports_folder_failure(tmp_path):
    """云盘目录创建失败时应记录失败。"""
    media = _library(tmp_path)
    STUBS.storage_chain.folder_fails = True
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False, max_retries=0)
    record = instance.mirror_file(STUBS.file_item(path=str(media), name=media.name), "", "入库")
    assert record["status"] == "fail"
    assert "目录创建失败" in record["detail"]


def test_mirror_file_skips_strm(tmp_path):
    """STRM 文件默认不镜像。"""
    season = tmp_path / "电视剧"
    season.mkdir()
    strm = season / "a.strm"
    strm.write_text("http://host/a.mkv", encoding="utf-8")
    instance = _plugin(source_roots=str(tmp_path), media_extensions=".strm,.mkv")
    assert instance.mirror_file(STUBS.file_item(path=str(strm), name="a.strm"), "", "入库") is None


def test_mirror_file_skips_non_media(tmp_path):
    """扩展名不在范围内的文件不镜像。"""
    media = _library(tmp_path)
    other = media.parent / "note.txt"
    other.write_bytes(b"x")
    instance = _plugin(source_roots=str(tmp_path))
    assert instance.mirror_file(STUBS.file_item(path=str(other), name="note.txt"), "", "入库") is None


def test_mirror_file_records_unmatched_root(tmp_path):
    """路径不在媒体库根目录下时记录失败，便于用户发现配置问题。"""
    outside = tmp_path / "a.mkv"
    outside.write_bytes(b"v")
    instance = _plugin(source_roots="/nowhere")
    record = instance.mirror_file(STUBS.file_item(path=str(outside), name="a.mkv"), "", "入库")
    assert record["status"] == "fail"
    assert "未匹配到媒体库根目录" in record["detail"]


def test_mirror_file_downloads_remote_library(tmp_path):
    """媒体库在远端存储时应先下载再上传。"""
    media = _library(tmp_path)
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False)
    item = STUBS.file_item(storage="u115", path=str(media), name=media.name)
    record = instance.mirror_file(item, "", "入库")
    assert record["status"] == "success"
    assert any(call[0] == "download_file" for call in STUBS.storage_chain.calls)


# ----------------------------------------------------------------- 过滤与事件


def test_matches_filters():
    """类型与分类过滤应同时生效，留空表示不过滤。"""
    instance = _plugin(include_types=["电视剧"], include_categories="国产剧")
    assert instance.matches_filters("电视剧", "国产剧") is True
    assert instance.matches_filters("电影", "国产剧") is False
    assert instance.matches_filters("电视剧", "日番") is False
    assert _plugin().matches_filters("电影", "华语电影") is True


def test_transfer_event_enqueues_media_files(tmp_path):
    """入库事件应把媒体文件加入镜像队列。"""
    media = _library(tmp_path)
    instance = _plugin(enabled=True, source_roots=str(tmp_path))
    try:
        transferinfo = types.SimpleNamespace(
            target_item=types.SimpleNamespace(path=str(media), storage="local"),
            file_list_new=[str(media), str(media.parent / "剧.nfo")])
        mediainfo = types.SimpleNamespace(title_year="兰香如故 (2026)", title="兰香如故",
                                         type=types.SimpleNamespace(value="电视剧"), category="国产剧")
        instance.on_transfer_complete(types.SimpleNamespace(
            event_data={"transferinfo": transferinfo, "mediainfo": mediainfo}))
        task = instance._queue.get_nowait()
        assert task["paths"] == [str(media)]
        assert task["title"] == "兰香如故 (2026)"
        assert task["storage"] == "local"
    finally:
        instance.stop_service()


def test_transfer_event_respects_category_filter(tmp_path):
    """分类不匹配时不入队。"""
    media = _library(tmp_path)
    instance = _plugin(enabled=True, source_roots=str(tmp_path), include_categories="日番")
    try:
        transferinfo = types.SimpleNamespace(
            target_item=types.SimpleNamespace(path=str(media), storage="local"), file_list_new=[])
        mediainfo = types.SimpleNamespace(title_year="兰香如故 (2026)", title="兰香如故",
                                         type=types.SimpleNamespace(value="电视剧"), category="国产剧")
        instance.on_transfer_complete(types.SimpleNamespace(
            event_data={"transferinfo": transferinfo, "mediainfo": mediainfo}))
        assert instance._queue.qsize() == 0
    finally:
        instance.stop_service()


def test_disabled_plugin_ignores_event(tmp_path):
    """未配置云盘目标时插件不应处理事件。"""
    instance = _plugin(enabled=True, target_root="")
    assert instance.get_state() is False
    transferinfo = types.SimpleNamespace(
        target_item=types.SimpleNamespace(path="/media/a.mkv", storage="local"), file_list_new=[])
    instance.on_transfer_complete(types.SimpleNamespace(
        event_data={"transferinfo": transferinfo, "mediainfo": None}))
    assert instance._queue.qsize() == 0


def test_plugin_action_triggers_scan():
    """插件动作事件应触发全量镜像。"""
    instance = _plugin(enabled=True)
    try:
        instance.on_plugin_action(types.SimpleNamespace(event_data={"action": "library_mirror"}))
        assert instance._queue.get_nowait()["type"] == "scan"
    finally:
        instance.stop_service()


def test_handle_task_mirrors_and_records(tmp_path):
    """任务处理后应写入历史。"""
    media = _library(tmp_path)
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False, delay=0)
    instance._handle_task({"paths": [str(media)], "storage": "local", "title": "T", "source": "入库"})
    history = instance.get_data("history")
    assert len(history) == 1
    assert history[0]["status"] == "success"


# ----------------------------------------------------------------- 全量扫描


def test_full_scan_uploads_missing_files(tmp_path):
    """全量扫描应补齐云盘缺失的媒体文件。"""
    media = _library(tmp_path)
    second = media.parent / "兰香如故 - S01E02.mkv"
    second.write_bytes(b"video-2")
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False, item_interval=0)
    instance.full_scan()
    remote = set(STUBS.storage_chain.remote_files)
    base = "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01"
    assert f"{base}/兰香如故 - S01E01.mkv" in remote
    assert f"{base}/兰香如故 - S01E02.mkv" in remote


def test_full_scan_respects_max_items(tmp_path):
    """单次处理上限应生效。"""
    media = _library(tmp_path)
    for index in range(2, 5):
        (media.parent / f"兰香如故 - S01E0{index}.mkv").write_bytes(b"v" * index)
    instance = _plugin(source_roots=str(tmp_path), copy_sidecars=False,
                       item_interval=0, max_items=2)
    instance.full_scan()
    assert len(STUBS.storage_chain.remote_files) == 2


def test_full_scan_without_roots_does_nothing(tmp_path):
    """没有可访问的本地根目录时不应上传。"""
    instance = _plugin(source_roots="/definitely/not/exist")
    instance.full_scan()
    assert not STUBS.storage_chain.remote_files


# ----------------------------------------------------------------- 宿主接口


def test_get_service_requires_state_and_cron():
    """只有启用且配置周期时才注册定时服务。"""
    assert _plugin().get_service() == []
    instance = _plugin(enabled=True, cron="0 4 * * *")
    try:
        assert instance.get_service()[0]["id"] == "LibraryMirror.FullScan"
    finally:
        instance.stop_service()


def test_get_service_rejects_invalid_cron():
    """非法周期表达式不注册服务。"""
    instance = _plugin(enabled=True, cron="bad cron")
    try:
        assert instance.get_service() == []
    finally:
        instance.stop_service()


def test_command_declaration():
    """远程命令声明应包含动作数据。"""
    command = plugin_module.LibraryMirror.get_command()[0]
    assert command["cmd"] == "/library_mirror"
    assert command["data"] == {"action": "library_mirror"}


def test_get_form_defaults_cover_all_models():
    """配置表单默认值应覆盖所有表单字段。"""
    form, defaults = _plugin().get_form()
    models = set()

    def walk(node):
        """递归收集 model 字段。"""
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


def test_get_page_states(tmp_path):
    """无历史显示提示，有历史渲染表格。"""
    instance = _plugin()
    assert instance.get_page()[0]["component"] == "VAlert"
    instance.save_data("history", [{"time": "t", "source": "入库", "title": "A",
                                    "status": "success", "remote": "/cloud/a.mkv", "detail": "ok"}])
    page = instance.get_page()
    assert page[0]["component"] == "VTable"
    assert len(page[0]["content"][1]["content"]) == 1


def test_api_endpoints():
    """API 应声明三个接口并返回三段式结构。"""
    instance = _plugin(enabled=True, source_roots="/media")
    try:
        apis = instance.get_api()
        assert [api["path"] for api in apis] == ["/status", "/history", "/mirror"]
        status = instance.api_status()
        assert status.success is True
        assert status.data["target_storage"] == "alist"
        assert status.data["library_roots"] == ["/media"]

        accepted = instance.api_mirror(path="/media/电影/a.mkv")
        assert accepted.success is True
        assert accepted.data["remote"] == "/cloud/media/电影/a.mkv"

        assert instance.api_mirror().success is False
        assert instance.api_mirror(path="/elsewhere/a.mkv").success is False
    finally:
        instance.stop_service()


def test_api_mirror_requires_enabled():
    """未启用时接口应拒绝。"""
    assert _plugin().api_mirror(path="/media/a.mkv").success is False


def test_notify_only_on_failure_by_default():
    """默认只在失败时推送通知。"""
    instance = _plugin(notify=True)
    instance._notify_records([{"status": "success", "title": "A", "remote": "/cloud/a.mkv"}])
    assert instance.messages == []
    instance._notify_records([{"status": "fail", "title": "B", "remote": "/cloud/b.mkv"}])
    assert len(instance.messages) == 1


def test_stop_service_is_idempotent():
    """停止服务可重复调用。"""
    instance = _plugin(enabled=True)
    instance.stop_service()
    instance.stop_service()
    assert instance._workers == []


def test_events_registered():
    """插件应注册入库与命令事件。"""
    names = {name for _, name in STUBS.event_manager.registered}
    assert {"on_transfer_complete", "on_plugin_action"}.issubset(names)
