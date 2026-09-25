"""在真实 MoviePilot V3 宿主中加载插件的验收测试。

覆盖开发指南要求的「真实加载检查」：插件可被宿主导入、继承宿主契约基类、
元数据完整、配置页与详情页可序列化、定时服务与命令声明正确、
启用后后台线程可回收、重复初始化不残留资源。
"""

from __future__ import annotations

import json
from importlib import import_module
from unittest.mock import Mock

import pytest

PLUGIN_MODULE = "app.plugins.strmprewarmer"


def _chain_context():
    """构造不启动宿主服务的最小 Chain 组合根。

    宿主 ``_PluginBase.__init__`` 会构造 PluginChain，而 ChainBase 要求启动
    组合根已装配运行上下文。测试里按官方插件仓的做法用 Mock 装配，
    只为让真实构造路径可以执行，不触碰任何宿主服务。
    """
    from app.application.chain.context import ChainRuntimeContext
    from app.application.configuration import ChainRuntimeConfig

    message_queue = Mock()
    message_queue.bind.return_value = Mock()
    return ChainRuntimeContext(
        module_manager=Mock(),
        plugin_manager=Mock(),
        event_manager=Mock(),
        message_oper=Mock(),
        message_helper=Mock(),
        file_cache=Mock(),
        async_file_cache=Mock(),
        message_queue=message_queue,
        module_dispatcher_factory=Mock(return_value=Mock()),
        site_repository=Mock(),
        subscription_repository=Mock(),
        subscription_mutation_scope=Mock(),
        sync_subscription_mutation_scope=Mock(),
        subscription_delete_scope=Mock(),
        sync_subscription_delete_scope=Mock(),
        subscription_completion_scope=Mock(),
        rule_group_mutation_scope=Mock(),
        site_reference_mutation_scope=Mock(),
        download_history_repository=Mock(),
        transfer_history_repository=Mock(),
        transfer_admission_repository=Mock(),
        transfer_execution_repository=Mock(),
        media_server_repository=Mock(),
        download_failure_repository=Mock(),
        user_repository=Mock(),
        configuration=ChainRuntimeConfig(media_extensions=(".mkv", ".strm")),
    )


@pytest.fixture(scope="module")
def plugin_class():
    """从宿主插件命名空间导入插件主类。"""
    module = import_module(PLUGIN_MODULE)
    return module.StrmPrewarmer


@pytest.fixture
def plugin(plugin_class):
    """用真实构造路径创建插件实例，并在用完后释放后台资源。"""
    from app.application.chain.context import configure_chain_runtime_context_provider

    configure_chain_runtime_context_provider(_chain_context)
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


def test_command_and_api_declarations(plugin, plugin_class):
    """命令声明正确，且未注册多余 API。"""
    plugin.init_plugin({"enabled": False})
    command = plugin_class.get_command()[0]
    assert command["cmd"] == "/strm_prewarm"
    assert command["data"] == {"action": "strm_prewarm"}
    assert plugin.get_api() == []


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
