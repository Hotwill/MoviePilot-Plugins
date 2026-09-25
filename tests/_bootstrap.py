"""插件仓单测引导薄壳：定位 MoviePilot 后端并委托主程序共享引导。

与官方插件仓保持同样的结构：本文件只负责找到后端目录并加入 ``sys.path``，
隔离 CONFIG_DIR、建表、暴露插件源码等逻辑都复用后端的 ``app.testing.bootstrap``。

后端位置按以下顺序解析：

1. 环境变量 ``MOVIEPILOT_BACKEND_PATH``；
2. 插件仓同级目录 ``MoviePilot``。

找不到后端时抛出 ``BackendNotFound``，由调用方决定跳过还是失败。
"""

from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
PLUGINS_REPO = _TESTS_DIR.parent
_WORKSPACE_ROOT = PLUGINS_REPO.parent


class BackendNotFound(RuntimeError):
    """未找到 MoviePilot 后端源码。"""


def resolve_backend_path() -> Path:
    """定位 MoviePilot 后端根目录。"""
    candidates = []
    env = os.environ.get("MOVIEPILOT_BACKEND_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(_WORKSPACE_ROOT / "MoviePilot")
    for path in candidates:
        if (path / "app").is_dir():
            return path
    raise BackendNotFound(
        "未找到 MoviePilot 后端（app/ 不存在）。请把后端放在插件仓同级目录，"
        f"或设置 MOVIEPILOT_BACKEND_PATH。已尝试：{[str(item) for item in candidates]}"
    )


def prepare_v3_backend() -> Path:
    """准备 V3 后端测试环境并暴露 ``plugins.v3`` 源码，返回后端路径。"""
    backend = resolve_backend_path()
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    bootstrap = import_module("app.testing.bootstrap")
    bootstrap.prepare_v3_backend(PLUGINS_REPO)
    return backend
