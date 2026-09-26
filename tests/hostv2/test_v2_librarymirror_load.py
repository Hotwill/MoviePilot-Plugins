"""在真实 MoviePilot V2 宿主中加载媒体库云盘镜像插件的验收测试。"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

from env import PLUGINS_REPO, resolve_backend

BACKEND_PATH = resolve_backend()


@pytest.fixture(scope="module")
def plugin_module():
    """把插件复制到 V2 宿主插件目录并按生产路径导入。"""
    target = Path(BACKEND_PATH) / "app" / "plugins" / "librarymirror"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(PLUGINS_REPO / "plugins.v2" / "librarymirror", target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "README.md"))
    try:
        yield importlib.import_module("app.plugins.librarymirror")
    finally:
        sys.modules.pop("app.plugins.librarymirror", None)
        shutil.rmtree(target, ignore_errors=True)


@pytest.fixture
def plugin(plugin_module):
    """创建插件实例并在用完后回收后台线程。"""
    instance = plugin_module.LibraryMirror()
    try:
        yield instance
    finally:
        instance.stop_service()


def test_inherits_v2_plugin_base(plugin_module):
    """插件必须继承 V2 宿主的 _PluginBase。"""
    from app.plugins import _PluginBase
    assert issubclass(plugin_module.LibraryMirror, _PluginBase)


def test_uses_v2_fallback_imports(plugin_module):
    """V2 上应落到旧路径导入分支。"""
    assert plugin_module.StorageHelper.__module__.startswith("app.helper.storage")
    assert plugin_module.DirectoryHelper.__module__.startswith("app.helper.directory")
    assert plugin_module._MsgType.__name__ == "NotificationType"


def test_events_registered_on_v2_bus(plugin_module):
    """入库与动作事件应登记到 V2 事件总线。"""
    from app.core.event import eventmanager
    identifiers = {item["handler_identifier"] for item in eventmanager.visualize_handlers()}
    for expected in ("LibraryMirror.on_transfer_complete", "LibraryMirror.on_plugin_action"):
        assert any(expected in item for item in identifiers), sorted(identifiers)


def test_lifecycle_and_paths_on_v2(plugin):
    """启用需要云盘配置；路径换算保持媒体库结构。"""
    plugin.init_plugin({"enabled": True})
    assert plugin.get_state() is False
    plugin.init_plugin({"enabled": True, "target_storage": "alist",
                        "target_root": "/cloud/media", "source_roots": "/media",
                        "cron": "0 4 * * *"})
    assert plugin.get_state() is True
    assert plugin.get_service()[0]["id"] == "LibraryMirror.FullScan"
    assert plugin.target_path("/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv") == \
        "/cloud/media/电视剧/国产剧/兰香如故 (2026)/Season 01/a.mkv"
    form, defaults = plugin.get_form()
    assert form and defaults
    assert plugin.get_page()
    plugin.stop_service()
    assert plugin._workers == []
