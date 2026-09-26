"""真实 MoviePilot V2 宿主加载测试的引导。

需要一份 MoviePilot V2 源码，通过 ``MOVIEPILOT_V2_BACKEND_PATH`` 或插件仓同级
``MoviePilot-v2`` 目录提供。V2 的站点资源 ``app.helper.sites`` 是按平台下发的
二进制制品，源码检出中不存在，这里按 V2 自带测试的做法补最小垫片。

必须与其它测试目录分开的 pytest 进程运行：V2 与 V3 的 ``app`` 包同名互斥，
``tests/v3`` 还会注入宿主桩模块。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import PLUGINS_REPO, resolve_backend  # noqa: E402


def _install_sites_stub() -> None:
    """注入站点资源垫片，V2 源码检出中缺少该二进制模块。"""
    stub = types.ModuleType("app.helper.sites")

    class SitesHelper:
        """测试用站点资源垫片。"""

        auth_level = 0

        def get_indexers(self) -> list:
            """返回空索引器列表。"""
            return []

        def get_indexer(self, *_args, **_kwargs):
            """返回空索引器详情。"""
            return None

    stub.SitesHelper = SitesHelper
    sys.modules.setdefault("app.helper.sites", stub)


if "app" in sys.modules and getattr(sys.modules["app"], "__file__", None) is None:
    pytest.skip("检测到宿主桩模块，V2 真实加载测试需要独立的 pytest 进程",
                allow_module_level=True)

try:
    BACKEND_PATH = resolve_backend()
except FileNotFoundError as error:
    pytest.skip(f"跳过 V2 真实加载测试：{error}", allow_module_level=True)

os.environ.setdefault("CONFIG_DIR", tempfile.mkdtemp(prefix="mp2-plugin-test-"))
for path in (str(BACKEND_PATH), str(PLUGINS_REPO / "plugins.v2")):
    if path not in sys.path:
        sys.path.insert(0, path)
_install_sites_stub()

try:
    from app.db.init import init_db

    init_db()
except Exception as error:  # pragma: no cover - 后端环境不完整时跳过而非误报
    pytest.skip(f"跳过 V2 真实加载测试：后端引导失败 {error}", allow_module_level=True)
