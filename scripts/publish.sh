#!/usr/bin/env bash
# 把本仓库发布到 GitHub。
#
# 用法：
#   bash scripts/publish.sh                # 使用默认 Hotwill/MoviePilot-Plugins
#   bash scripts/publish.sh <用户名/仓库名>
#
# 前置条件（二者之一）：
#   1. 已执行 gh auth login（脚本会自动创建仓库）；或
#   2. 已在 GitHub 网页手动创建同名空仓库（不要初始化 README）。
# 推送使用 SSH，需要本机 SSH key 已加入 GitHub 账号。

set -euo pipefail

TARGET="${1:-Hotwill/MoviePilot-Plugins}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "== 目标仓库：${TARGET}"

if git ls-remote "git@github.com:${TARGET}.git" >/dev/null 2>&1; then
  echo "== 远端仓库已存在，直接推送"
else
  echo "== 远端仓库不存在，尝试用 gh 创建"
  if ! gh auth status >/dev/null 2>&1; then
    echo "!! gh 未登录。请先执行：gh auth login" >&2
    echo "!! 或在 GitHub 网页创建空仓库 ${TARGET} 后重新运行本脚本" >&2
    exit 1
  fi
  gh repo create "${TARGET}" --public \
    --description "MoviePilot 第三方插件仓库：STRM 媒体信息预热" || true
fi

git remote remove origin 2>/dev/null || true
git remote add origin "git@github.com:${TARGET}.git"
git push -u origin main

echo
echo "== 发布完成。在 MoviePilot 中添加插件仓库地址："
echo "   https://github.com/${TARGET}"
