"""V2 真实加载测试的环境定位。"""

from __future__ import annotations

import os
from pathlib import Path

PLUGINS_REPO = Path(__file__).resolve().parents[2]


def resolve_backend() -> Path:
    """定位 MoviePilot V2 源码目录。"""
    candidates = []
    env = os.environ.get("MOVIEPILOT_V2_BACKEND_PATH")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(PLUGINS_REPO.parent / "MoviePilot-v2")
    for path in candidates:
        if (path / "app" / "core" / "event.py").is_file():
            return path
    raise FileNotFoundError(
        "未找到 MoviePilot V2 源码，请设置 MOVIEPILOT_V2_BACKEND_PATH 或放置在同级 MoviePilot-v2 目录"
    )
