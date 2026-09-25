#!/usr/bin/env python3
"""同步插件实现到各代目录，并校验索引与代码版本一致。

插件源码以 ``plugins.v3/<id>/`` 为准，实现内部通过导入回退同时兼容
MoviePilot V2 与 V3 宿主，因此 ``plugins.v2/`` 与 ``plugins/`` 只是同一份
源码的镜像。执行本脚本可保证三份实现与三个索引文件不产生漂移。
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "plugins.v3"
MIRROR_DIRS = (ROOT / "plugins.v2", ROOT / "plugins")
INDEX_FILES = {
    "plugins.v3": ROOT / "package.v3.json",
    "plugins.v2": ROOT / "package.v2.json",
    "plugins": ROOT / "package.json",
}


def plugin_ids() -> list:
    """返回源目录中的插件目录名列表。"""
    return sorted(path.name for path in SOURCE_DIR.iterdir()
                  if path.is_dir() and (path / "__init__.py").exists())


def read_metadata(init_file: Path) -> dict:
    """从插件源码中解析类名与版本号。"""
    text = init_file.read_text(encoding="utf-8")
    class_match = re.search(r"^class\s+(\w+)\(_PluginBase\)", text, re.MULTILINE)
    version_match = re.search(r'^\s+plugin_version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return {
        "class": class_match.group(1) if class_match else None,
        "version": version_match.group(1) if version_match else None,
    }


def sync(plugin_id: str) -> None:
    """把源实现复制到其它代目录。"""
    source = SOURCE_DIR / plugin_id
    for mirror_root in MIRROR_DIRS:
        target = mirror_root / plugin_id
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        print(f"同步 {source.relative_to(ROOT)} -> {target.relative_to(ROOT)}")


def check(plugin_id: str) -> list:
    """校验各代实现内容一致，且索引版本与代码版本匹配。"""
    problems = []
    source_file = SOURCE_DIR / plugin_id / "__init__.py"
    metadata = read_metadata(source_file)
    if not metadata["class"]:
        problems.append(f"{plugin_id}: 未找到继承 _PluginBase 的主类")
        return problems
    if metadata["class"].lower() != plugin_id:
        problems.append(f"{plugin_id}: 目录名必须是主类 {metadata['class']} 的小写形式")
    if not metadata["version"]:
        problems.append(f"{plugin_id}: 未找到 plugin_version")
        return problems

    source_text = source_file.read_text(encoding="utf-8")
    for mirror_root in MIRROR_DIRS:
        mirror_file = mirror_root / plugin_id / "__init__.py"
        if not mirror_file.exists():
            problems.append(f"{mirror_root.name}/{plugin_id}: 缺少实现，请执行同步")
        elif mirror_file.read_text(encoding="utf-8") != source_text:
            problems.append(f"{mirror_root.name}/{plugin_id}: 实现与 plugins.v3 不一致，请执行同步")

    for directory, index_file in INDEX_FILES.items():
        if not index_file.exists():
            problems.append(f"{index_file.name}: 索引文件缺失")
            continue
        index = json.loads(index_file.read_text(encoding="utf-8"))
        entry = index.get(metadata["class"])
        if not entry:
            problems.append(f"{index_file.name}: 缺少 {metadata['class']} 条目")
            continue
        if entry.get("version") != metadata["version"]:
            problems.append(
                f"{index_file.name}: version {entry.get('version')} 与代码 {metadata['version']} 不一致")
        history = entry.get("history") or {}
        if f"v{metadata['version']}" not in history:
            problems.append(f"{index_file.name}: history 缺少 v{metadata['version']}")
        elif list(history)[0] != f"v{metadata['version']}":
            problems.append(f"{index_file.name}: history 需把当前版本置顶")
    return problems


def main() -> int:
    """命令行入口：同步或校验插件实现。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验不写入")
    args = parser.parse_args()

    ids = plugin_ids()
    if not ids:
        print("没有找到插件实现")
        return 1
    if not args.check:
        for plugin_id in ids:
            sync(plugin_id)
    problems = []
    for plugin_id in ids:
        problems.extend(check(plugin_id))
    if problems:
        for problem in problems:
            print(f"错误: {problem}")
        return 1
    print(f"校验通过：{', '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
