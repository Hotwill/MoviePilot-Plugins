"""插件源码加载器：装好宿主桩模块后按生产模块名加载插件。

所有 V3 插件测试共用本模块，保证一个 pytest 进程内桩模块只安装一次、
每个插件只加载一次，避免事件注册等导入期副作用被重复执行或覆盖。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from host_stubs import install_stubs

REPO_ROOT = Path(__file__).resolve().parents[2]
STUBS = install_stubs()


def load_plugin(plugin_id: str) -> ModuleType:
    """按 ``app.plugins.<plugin_id>`` 加载 ``plugins.v3/<plugin_id>/__init__.py``。"""
    module_name = f"app.plugins.{plugin_id}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    source = REPO_ROOT / "plugins.v3" / plugin_id / "__init__.py"
    spec = importlib.util.spec_from_file_location(module_name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
