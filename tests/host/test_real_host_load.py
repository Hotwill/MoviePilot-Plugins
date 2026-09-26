"""在真实 MoviePilot V3 宿主中加载插件的验收测试。

覆盖开发指南要求的「真实加载检查」：插件可被宿主导入、继承宿主契约基类、
元数据完整、配置页与详情页可序列化、定时服务与命令声明正确、
启用后后台线程可回收、重复初始化不残留资源。
"""

from __future__ import annotations

import json
from importlib import import_module
import pytest

from chain_stub import chain_context

PLUGIN_MODULE = "app.plugins.strmprewarmer"


@pytest.fixture(scope="module")
def plugin_class():
    """从宿主插件命名空间导入插件主类。"""
    module = import_module(PLUGIN_MODULE)
    return module.StrmPrewarmer


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


def test_plugin_inherits_host_base(plugin_class):
    """插件必须继承宿主提供的 _PluginBase。"""
    host_base = import_module("app.plugins")._PluginBase
    assert issubclass(plugin_class, host_base)


def test_plugin_metadata_complete(plugin_class):
    """插件元数据必须齐全，且目录名是类名小写。"""
    for attribute in ("plugin_name", "plugin_desc", "plugin_version", "plugin_author",
                      "author_url", "plugin_config_prefix", "plugin_icon"):
        assert getattr(plugin_class, attribute, None), f"缺少元数据 {attribute}"
    assert plugin_class.__name__ == "StrmPrewarmer"
    assert import_module(PLUGIN_MODULE).__name__.rsplit(".", 1)[-1] == plugin_class.__name__.lower()


def test_plugin_version_matches_index(plugin_class):
    """代码版本必须与 package.v3.json 中的版本一致。"""
    from _bootstrap import PLUGINS_REPO
    index = json.loads((PLUGINS_REPO / "package.v3.json").read_text(encoding="utf-8"))
    entry = index[plugin_class.__name__]
    assert entry["version"] == plugin_class.plugin_version
    assert list(entry["history"])[0] == f"v{plugin_class.plugin_version}"


def test_form_and_page_serializable(plugin):
    """配置页与详情页必须可 JSON 序列化，且表单字段都有默认值。"""
    plugin.init_plugin({"enabled": False})
    form, defaults = plugin.get_form()
    json.dumps(form, ensure_ascii=False)
    models = _form_models(form)
    assert models and models.issubset(set(defaults))
    page = plugin.get_page()
    json.dumps(page, ensure_ascii=False)
    assert page


def test_command_declaration(plugin, plugin_class):
    """命令声明正确。"""
    plugin.init_plugin({"enabled": False})
    command = plugin_class.get_command()[0]
    assert command["cmd"] == "/strm_prewarm"
    assert command["data"] == {"action": "strm_prewarm"}


def test_api_declarations_register_on_fastapi(plugin):
    """插件 API 声明必须能在宿主的 FastAPI 上真实注册并生成 OpenAPI。"""
    from fastapi import FastAPI

    plugin.init_plugin({"enabled": False})
    apis = plugin.get_api()
    assert [api["path"] for api in apis] == ["/status", "/history", "/prewarm"]

    app = FastAPI()
    for api in apis:
        app.add_api_route(
            f"/api/v1/plugin/StrmPrewarmer{api['path']}",
            api["endpoint"],
            methods=api["methods"],
            response_model=api["response_model"],
            summary=api["summary"],
        )
    schema = app.openapi()
    paths = schema["paths"]
    assert "/api/v1/plugin/StrmPrewarmer/status" in paths
    assert "/api/v1/plugin/StrmPrewarmer/prewarm" in paths
    # 响应模型必须暴露三段式结构，而不是被隐藏
    properties = schema["components"]["schemas"]["ApiResult"]["properties"]
    assert {"success", "message", "data"}.issubset(set(properties))


def test_api_endpoints_return_expected_shape(plugin):
    """通过 TestClient 实际调用插件 API，校验返回结构。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin.init_plugin({"enabled": True})
    app = FastAPI()
    for api in plugin.get_api():
        app.add_api_route(f"/plugin{api['path']}", api["endpoint"],
                          methods=api["methods"], response_model=api["response_model"])
    client = TestClient(app)

    status = client.get("/plugin/status").json()
    assert status["success"] is True
    assert status["data"]["enabled"] is True

    accepted = client.post("/plugin/prewarm", params={"path": "/media/strm/a.strm"}).json()
    assert accepted["success"] is True
    assert accepted["data"]["path"] == "/media/strm/a.strm"

    rejected = client.post("/plugin/prewarm", params={"path": "/media/a.mkv"}).json()
    assert rejected["success"] is False

    history = client.get("/plugin/history", params={"limit": 5}).json()
    assert history["success"] is True
    assert "records" in history["data"]
    plugin.stop_service()


def test_service_registration_and_thread_lifecycle(plugin):
    """启用后注册定时服务并启动后台线程，停用后线程回收。"""
    plugin.init_plugin({"enabled": True, "cron": "0 3 * * *"})
    assert plugin.get_state() is True
    services = plugin.get_service()
    assert services[0]["id"] == "StrmPrewarmer.FullScan"
    assert callable(services[0]["func"])
    assert plugin._workers and plugin._workers[0].is_alive()
    plugin.stop_service()
    assert plugin._workers == []


def test_repeated_init_does_not_leak_threads(plugin):
    """重复初始化不应累积后台线程。"""
    for _ in range(3):
        plugin.init_plugin({"enabled": True})
    assert len(plugin._workers) == 1
    plugin.stop_service()
    assert plugin._workers == []


def test_disabled_plugin_registers_nothing(plugin):
    """未启用时不注册定时服务也不启动线程。"""
    plugin.init_plugin({"enabled": False, "cron": "0 3 * * *"})
    assert plugin.get_state() is False
    assert plugin.get_service() == []
    assert plugin._workers == []


def test_event_handlers_registered_with_host(plugin_class):
    """插件的事件处理器应真实登记到宿主事件总线上。"""
    from app.runtime.events import eventmanager
    from app.schemas.types import EventType

    handlers = {name for name in dir(plugin_class) if name.startswith("on_")}
    assert {"on_transfer_complete", "on_webhook_message", "on_plugin_action"}.issubset(handlers)

    registrations = eventmanager.visualize_handlers()
    identifiers = {item["handler_identifier"] for item in registrations}
    for expected in ("StrmPrewarmer.on_transfer_complete",
                    "StrmPrewarmer.on_webhook_message",
                    "StrmPrewarmer.on_plugin_action"):
        assert any(expected in identifier for identifier in identifiers), \
            f"未在宿主事件总线找到 {expected}：{sorted(identifiers)}"

    for event_type in (EventType.TransferComplete, EventType.WebhookMessage, EventType.PluginAction):
        assert eventmanager.check(event_type), f"{event_type} 没有可用处理器"


def test_plugin_data_roundtrip(plugin):
    """插件数据读写应走宿主数据接口。"""
    plugin.init_plugin({"enabled": False})
    plugin.save_data("history", [{"time": "t", "title": "A", "status": "success"}])
    assert plugin.get_data("history")[0]["title"] == "A"
    plugin.del_data("history")
    assert not plugin.get_data("history")
