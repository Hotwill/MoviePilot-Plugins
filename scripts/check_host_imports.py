#!/usr/bin/env python3
"""校验插件对宿主的导入在真实 MoviePilot 源码中可以解析。

插件需要同时兼容 MoviePilot V2 与 V3，两代宿主的导入路径不同（V3 走
``app.sdk.*``，V2 走 ``app.core.*`` / ``app.helper.*`` / ``app.utils.*``）。
本脚本不执行宿主代码，只用 AST 在指定的 MoviePilot 源码目录中解析符号，
用于在没有宿主运行环境时快速发现导入路径写错的问题。

用法::

    python3 scripts/check_host_imports.py /path/to/MoviePilot [更多宿主目录...]
"""

import argparse
import ast
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = ROOT / "plugins.v3" / "strmprewarmer" / "__init__.py"

# V3 优先使用的 SDK 导入；V2 宿主允许缺失
V3_MODULES = {"app.sdk.events", "app.sdk.logging", "app.sdk.network", "app.sdk.services"}
# V2 回退导入；V3 宿主通过兼容层承接
V2_MODULES = {"app.core.event", "app.log", "app.utils.http", "app.helper.mediaserver"}
# 两代宿主的消息类型枚举名不同，插件只需其中之一
MESSAGE_TYPES = {"MessageType", "NotificationType"}


def plugin_imports() -> List[Tuple[str, str]]:
    """从插件源码中收集所有 app.* 的 from-import 符号。"""
    tree = ast.parse(PLUGIN_FILE.read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            for alias in node.names:
                imports.append((node.module, alias.name))
    return sorted(set(imports))


def module_file(host: Path, module: str) -> Optional[Path]:
    """把模块名解析为宿主源码中的文件路径。"""
    relative = Path(*module.split("."))
    for candidate in (host / relative.with_suffix(".py"), host / relative / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def defined_names(path: Path, host: Path = None, depth: int = 0) -> Set[str]:
    """收集一个模块文件中可被外部导入的名字，必要时展开 ``import *``。"""
    names: Set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            if node.names and node.names[0].name == "*":
                # 展开星号导入，V2 的 app.schemas 通过该方式聚合子模块符号
                if host is not None and depth < 2:
                    target = node.module or ""
                    if node.level:
                        package = path.parent.relative_to(host).as_posix().replace("/", ".")
                        target = f"{package}.{target}" if target else package
                    star_path = module_file(host, target)
                    if star_path and star_path != path:
                        names |= defined_names(star_path, host, depth + 1)
                continue
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def lazy_export_names(path: Path) -> Set[str]:
    """收集惰性导出模块（如 app.schemas）在映射表中声明的符号。"""
    names: Set[str] = set()
    text = path.read_text(encoding="utf-8", errors="replace")
    if "SCHEMA_EXPORTS" not in text and "__getattr__" not in text:
        return names
    for sibling in (path.parent / "exports.py", path.parent / "_exports.py"):
        if not sibling.exists():
            continue
        tree = ast.parse(sibling.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        names.add(key.value)
    return names


def compat_symbols(host: Path) -> Dict[str, Set[str]]:
    """解析 V3 兼容清单，得到模块到兼容符号的映射。"""
    manifest = host / "app" / "runtime" / "compat" / "manifest.py"
    if not manifest.exists():
        return {}
    text = manifest.read_text(encoding="utf-8", errors="replace")
    result: Dict[str, Set[str]] = {}
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not key.value.startswith("app."):
                continue
            symbols: Set[str] = set()
            if isinstance(value, ast.Dict):
                for symbol_key in value.keys:
                    if isinstance(symbol_key, ast.Constant) and isinstance(symbol_key.value, str):
                        symbols.add(symbol_key.value)
            else:
                # ModuleAlias(...) 表示整模块兼容，任何符号都由目标模块承接
                symbols.add("*")
            result.setdefault(key.value, set()).update(symbols)
    return result


def resolve(host: Path, module: str, symbol: str, compat: Dict[str, Set[str]]) -> Tuple[bool, str]:
    """判断 (module, symbol) 能否在宿主中解析，并返回解析方式。"""
    path = module_file(host, module)
    if path:
        names = defined_names(path, host) | lazy_export_names(path)
        if symbol in names:
            return True, f"{path.relative_to(host)}"
        compat_names = compat.get(module, set())
        if symbol in compat_names or "*" in compat_names:
            return True, "兼容清单"
        return False, f"{path.relative_to(host)} 中未定义"
    compat_names = compat.get(module, set())
    if symbol in compat_names or "*" in compat_names:
        return True, "兼容清单"
    return False, "模块不存在"


def check_host(host: Path) -> bool:
    """校验单个宿主目录，返回是否通过。"""
    compat = compat_symbols(host)
    generation = "V3" if (host / "app" / "sdk").exists() else "V2"
    print(f"\n== 宿主 {host}（判定为 {generation}）")
    groups = {"V3 SDK": [], "V2 兼容": [], "消息类型": [], "公共": []}
    for module, symbol in plugin_imports():
        ok, how = resolve(host, module, symbol, compat)
        if module in V3_MODULES:
            group = "V3 SDK"
        elif module in V2_MODULES:
            group = "V2 兼容"
        elif symbol in MESSAGE_TYPES:
            # V3 提供 MessageType，V2 提供 NotificationType，插件按代选择其一
            group = "消息类型"
        else:
            group = "公共"
        groups[group].append((module, symbol, ok, how))
        print(f"  [{'OK ' if ok else 'FAIL'}] {group:7} {module}.{symbol} -> {how}")

    shared_ok = all(ok for _, _, ok, _ in groups["公共"])
    message_ok = any(ok for _, _, ok, _ in groups["消息类型"])
    v3_ok = bool(groups["V3 SDK"]) and all(ok for _, _, ok, _ in groups["V3 SDK"])
    v2_ok = bool(groups["V2 兼容"]) and all(ok for _, _, ok, _ in groups["V2 兼容"])
    if not shared_ok:
        print("  结果: 失败（公共导入无法解析）")
        return False
    if not message_ok:
        print("  结果: 失败（MessageType 与 NotificationType 都无法解析）")
        return False
    if not (v3_ok or v2_ok):
        print("  结果: 失败（两条导入分支都无法解析）")
        return False
    branch = "V3 SDK" if v3_ok else "V2 兼容"
    print(f"  结果: 通过（使用 {branch} 分支，公共导入与消息类型均可解析）")
    return True


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hosts", nargs="+", type=Path, help="MoviePilot 源码目录")
    args = parser.parse_args()
    results = []
    for host in args.hosts:
        if not (host / "app").exists():
            print(f"跳过 {host}：不是 MoviePilot 源码目录")
            results.append(False)
            continue
        results.append(check_host(host.resolve()))
    print()
    if all(results):
        print("全部宿主校验通过")
        return 0
    print("存在校验失败的宿主")
    return 1


if __name__ == "__main__":
    sys.exit(main())
