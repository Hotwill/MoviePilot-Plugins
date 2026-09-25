"""真实宿主加载测试的引导。

这些测试需要一份真实的 MoviePilot V3 源码（通过 ``MOVIEPILOT_BACKEND_PATH``
或插件仓同级 ``MoviePilot`` 目录提供）。没有后端时整体跳过，
保证 ``pytest tests/v3`` 这类纯逻辑测试在任何环境都能运行。

注意：必须与 ``tests/v3`` 分开的 pytest 进程运行——后者会注入宿主桩模块，
同一进程内两者会互相污染 ``sys.modules``。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from _bootstrap import BackendNotFound, prepare_v3_backend
except ImportError as error:  # pragma: no cover - 引导缺失属于仓库结构问题
    raise RuntimeError(f"无法加载测试引导：{error}") from error

if "app" in sys.modules and getattr(sys.modules["app"], "__file__", None) is None:
    pytest.skip("检测到宿主桩模块，真实加载测试需要独立的 pytest 进程",
                allow_module_level=True)

try:
    BACKEND_PATH = prepare_v3_backend()
except BackendNotFound as error:
    pytest.skip(f"跳过真实宿主加载测试：{error}", allow_module_level=True)
except Exception as error:  # pragma: no cover - 后端环境不完整时跳过而非误报
    pytest.skip(f"跳过真实宿主加载测试：后端引导失败 {error}", allow_module_level=True)
