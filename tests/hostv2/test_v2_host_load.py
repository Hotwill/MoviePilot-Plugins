"""在真实 MoviePilot V2 宿主中加载插件的验收测试。

重点验证同一份实现在 V2 上会走旧路径回退分支，并且生命周期契约成立。
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from conftest import BACKEND_PATH, PLUGINS_REPO


@pytest.fixture(scope="module")
def plugin_module():
    """把插件复制到 V2 宿主插件目录并按生产路径导入。"""
    target = Path(BACKEND_PATH) / "app" / "plugins" / "strmprewarmer"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PLUGINS_REPO / "plugins.v2" / "strmprewarmer", target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "README.md"))
    try:
        yield importlib.import_module("app.plugins.strmprewarmer")
    finally:
        sys.modules.pop("app.plugins.strmprewarmer", None)
        shutil.rmtree(target, ignore_errors=True)


@pytest.fixture
def plugin(plugin_module):
    """创建插件实例并在用完后回收后台线程。"""
    instance = plugin_module.StrmPrewarmer()
    try:
        yield instance
    finally:
        instance.stop_service()


def test_inherits_v2_plugin_base(plugin_module):
    """插件必须继承 V2 宿主的 _PluginBase。"""
    from app.plugins import _PluginBase
    assert issubclass(plugin_module.StrmPrewarmer, _PluginBase)


def test_uses_v2_fallback_imports(plugin_module):
    """V2 上应落到旧路径导入分支，而不是 V3 的 app.sdk。"""
    assert plugin_module.RequestUtils.__module__.startswith("app.utils.http")
    assert plugin_module.MediaServerHelper.__module__.startswith("app.helper.mediaserver")
    assert plugin_module._MsgType.__name__ == "NotificationType"


def test_events_registered_on_v2_bus(plugin_module):
    """事件处理器应登记到 V2 事件总线。"""
    from app.core.event import eventmanager
    from app.schemas.types import EventType

    identifiers = {item["handler_identifier"] for item in eventmanager.visualize_handlers()}
    for expected in ("StrmPrewarmer.on_transfer_complete",
                     "StrmPrewarmer.on_webhook_message",
                     "StrmPrewarmer.on_plugin_action"):
        assert any(expected in item for item in identifiers), sorted(identifiers)
    for event_type in (EventType.TransferComplete, EventType.WebhookMessage, EventType.PluginAction):
        assert eventmanager.check(event_type)


def test_lifecycle_on_v2(plugin):
    """启用后注册定时服务并启动线程，停用后回收。"""
    plugin.init_plugin({"enabled": True, "cron": "0 3 * * *"})
    assert plugin.get_state() is True
    assert plugin.get_service()[0]["id"] == "StrmPrewarmer.FullScan"
    assert plugin._workers and plugin._workers[0].is_alive()
    plugin.stop_service()
    assert plugin._workers == []


def test_form_and_page_on_v2(plugin):
    """配置页与详情页在 V2 上同样可用。"""
    plugin.init_plugin({"enabled": False})
    form, defaults = plugin.get_form()
    assert form and defaults
    assert plugin.get_page()
    assert [api["path"] for api in plugin.get_api()] == ["/status", "/history", "/prewarm"]
    assert plugin_module_command(plugin) == "/strm_prewarm"


def plugin_module_command(plugin) -> str:
    """读取插件声明的远程命令。"""
    return type(plugin).get_command()[0]["cmd"]


def test_api_endpoints_on_v2(plugin):
    """V2 上插件 API 同样可调用并返回三段式结构。"""
    plugin.init_plugin({"enabled": True})
    status = plugin.api_status()
    assert status.success is True and status.data["enabled"] is True
    accepted = plugin.api_prewarm(path="/media/strm/a.strm")
    assert accepted.success is True
    assert plugin._queue.get_nowait()["source"] == "API"
    plugin.stop_service()
