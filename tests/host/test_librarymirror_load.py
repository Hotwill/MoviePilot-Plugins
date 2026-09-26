"""在真实 MoviePilot V3 宿主中加载媒体库云盘镜像插件的验收测试。"""

from __future__ import annotations

import json
from importlib import import_module

import pytest

from chain_stub import chain_context

PLUGIN_MODULE = "app.plugins.librarymirror"


@pytest.fixture(scope="module")
def plugin_class():
    """从宿主插件命名空间导入插件主类。"""
    return import_module(PLUGIN_MODULE).LibraryMirror


@pytest.fixture
def plugin(plugin_class):
    """用真实构造路径创建插件实例，并在用完后释放后台资源。"""
    from app.application.chain.context import configure_chain_runtime_context_provider

    configure_chain_runtime_context_provider(chain_context)
    instance = plugin_class()
    try:
        yield instance
    finally:
        instance.stop_service()
        configure_chain_runtime_context_provider(None)


def _form_models(node, found=None) -> set:
    """递归收集配置页中的 model 字段。"""
    found = set() if found is None else found
    if isinstance(node, dict):
        model = (node.get("props") or {}).get("model")
        if model:
            found.add(model)
        for value in node.values():
            _form_models(value, found)
    elif isinstance(node, list):
        for value in node:
            _form_models(value, found)
    return found


def test_inherits_host_base(plugin_class):
    """插件必须继承宿主提供的 _PluginBase。"""
    assert issubclass(plugin_class, import_module("app.plugins")._PluginBase)


def test_metadata_and_version_match_index(plugin_class):
    """元数据齐全，且代码版本与 package.v3.json 一致。"""
    from _bootstrap import PLUGINS_REPO

    for attribute in ("plugin_name", "plugin_desc", "plugin_version", "plugin_author",
                      "author_url", "plugin_config_prefix", "plugin_icon"):
        assert getattr(plugin_class, attribute, None), f"缺少元数据 {attribute}"
    index = json.loads((PLUGINS_REPO / "package.v3.json").read_text(encoding="utf-8"))
    entry = index[plugin_class.__name__]
    assert entry["version"] == plugin_class.plugin_version
    assert list(entry["history"])[0] == f"v{plugin_class.plugin_version}"


def test_state_requires_target_config(plugin):
    """未配置云盘存储或根目录时插件不应进入启用态。"""
    plugin.init_plugin({"enabled": True})
    assert plugin.get_state() is False
    assert plugin._workers == []
    plugin.init_plugin({"enabled": True, "target_storage": "alist", "target_root": "/cloud"})
    assert plugin.get_state() is True
    assert plugin._workers and plugin._workers[0].is_alive()


def test_form_and_page_serializable(plugin):
    """配置页与详情页必须可 JSON 序列化，且表单字段都有默认值。"""
    plugin.init_plugin({"enabled": False})
    form, defaults = plugin.get_form()
    json.dumps(form, ensure_ascii=False)
    models = _form_models(form)
    assert models and models.issubset(set(defaults))
    json.dumps(plugin.get_page(), ensure_ascii=False)


def test_library_roots_read_from_host_directory_config(plugin):
    """未填根目录时应能调用宿主目录配置接口而不报错。"""
    plugin.init_plugin({"enabled": False})
    roots = plugin.library_roots()
    assert isinstance(roots, tuple)


def test_target_path_keeps_structure(plugin):
    """云盘路径应保持媒体库的多级目录结构。"""
    plugin.init_plugin({"enabled": True, "target_storage": "alist",
                        "target_root": "/cloud/media", "source_roots": "/media"})
    assert plugin.target_path("/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv") == \
        "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv"


def test_service_and_command(plugin, plugin_class):
    """定时服务与远程命令声明正确。"""
    plugin.init_plugin({"enabled": True, "target_storage": "alist",
                        "target_root": "/cloud", "cron": "0 4 * * *"})
    services = plugin.get_service()
    assert services[0]["id"] == "LibraryMirror.FullScan"
    assert callable(services[0]["func"])
    command = plugin_class.get_command()[0]
    assert command["cmd"] == "/library_mirror"


def test_api_registers_on_fastapi(plugin):
    """插件 API 声明必须能在 FastAPI 上注册并实调返回三段式结构。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin.init_plugin({"enabled": True, "target_storage": "alist",
                        "target_root": "/cloud/media", "source_roots": "/media"})
    app = FastAPI()
    for api in plugin.get_api():
        app.add_api_route(f"/plugin{api['path']}", api["endpoint"],
                          methods=api["methods"], response_model=api["response_model"])
    client = TestClient(app)

    status = client.get("/plugin/status").json()
    assert status["success"] is True
    assert status["data"]["target_root"] == "/cloud/media"

    accepted = client.post("/plugin/mirror", params={"path": "/media/电影/a.mkv"}).json()
    assert accepted["success"] is True
    assert accepted["data"]["remote"] == "/cloud/media/电影/a.mkv"

    rejected = client.post("/plugin/mirror", params={"path": "/outside/a.mkv"}).json()
    assert rejected["success"] is False


def test_events_registered_with_host(plugin_class):
    """入库与插件动作事件应登记到宿主事件总线。"""
    from app.runtime.events import eventmanager
    from app.schemas.types import EventType

    identifiers = {item["handler_identifier"] for item in eventmanager.visualize_handlers()}
    for expected in ("LibraryMirror.on_transfer_complete", "LibraryMirror.on_plugin_action"):
        assert any(expected in item for item in identifiers), sorted(identifiers)
    assert eventmanager.check(EventType.TransferComplete)


def test_repeated_init_does_not_leak_threads(plugin):
    """重复初始化不应累积后台线程。"""
    for _ in range(3):
        plugin.init_plugin({"enabled": True, "target_storage": "alist", "target_root": "/cloud"})
    assert len(plugin._workers) == 1
    plugin.stop_service()
    assert plugin._workers == []
