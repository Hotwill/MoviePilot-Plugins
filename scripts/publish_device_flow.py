#!/usr/bin/env python3
"""用 GitHub 设备码流程完成登录，并自动创建仓库、推送代码。

设计目标：在非交互环境中把「需要人工介入的部分」压缩成一个验证码。
脚本会申请设备码并写入日志，用户在 https://github.com/login/device 输入验证码
授权后，脚本自动拿到访问令牌、写入 gh 凭据、创建仓库并推送 main 分支。
验证码过期会自动换新，最多循环若干轮。令牌不会写入日志。
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# GitHub CLI 的公开 OAuth 客户端 ID，设备码流程无需客户端密钥
CLIENT_ID = "178c6fc778ccc68e1d6a"
SCOPES = "repo workflow"
REPO = os.environ.get("PUBLISH_REPO", "Hotwill/MoviePilot-Plugins")
REPO_ROOT = Path(__file__).resolve().parent
LOG_FILE = Path(os.environ.get("PUBLISH_LOG", "/tmp/mpwork/publish_auto.log"))
MAX_CYCLES = int(os.environ.get("PUBLISH_CYCLES", "8"))


def log(message: str) -> None:
    """追加一行带时间戳的日志。"""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def post(url: str, payload: dict) -> dict:
    """向 GitHub 提交表单请求并解析 JSON 响应。"""
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def request_device_code() -> dict:
    """申请新的设备码。"""
    return post("https://github.com/login/device/code",
                {"client_id": CLIENT_ID, "scope": SCOPES})


def poll_token(device_code: str, interval: int, expires_in: int) -> str:
    """轮询授权结果，成功返回访问令牌，超时返回空字符串。"""
    deadline = time.time() + expires_in
    wait = max(interval, 5)
    while time.time() < deadline:
        time.sleep(wait)
        try:
            result = post("https://github.com/login/oauth/access_token", {
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
        except Exception as error:
            log(f"轮询出错，稍后重试：{error}")
            continue
        if result.get("access_token"):
            return result["access_token"]
        error = result.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            wait += 5
            continue
        if error in ("expired_token", "access_denied", "device_flow_disabled"):
            log(f"授权结束：{error}")
            return ""
        log(f"未知响应：{error}")
    return ""


def run(command: list, token: str = None, stdin: str = None) -> subprocess.CompletedProcess:
    """执行子进程命令，必要时注入令牌环境变量。"""
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    return subprocess.run(command, cwd=REPO_ROOT, env=env, input=stdin,
                          capture_output=True, text=True, timeout=300)


def publish(token: str) -> bool:
    """写入 gh 凭据，创建仓库并推送代码。"""
    login = run(["gh", "auth", "login", "--hostname", "github.com", "--with-token"], stdin=token)
    if login.returncode != 0:
        log(f"写入 gh 凭据失败：{login.stderr.strip()[:200]}")
    else:
        log("已写入 gh 凭据")

    who = run(["gh", "api", "user", "--jq", ".login"], token=token)
    if who.returncode != 0:
        log(f"令牌校验失败：{who.stderr.strip()[:200]}")
        return False
    log(f"令牌校验通过，账号：{who.stdout.strip()}")

    exists = run(["git", "ls-remote", f"git@github.com:{REPO}.git"])
    if exists.returncode != 0:
        create = run(["gh", "repo", "create", REPO, "--public", "--description",
                      "MoviePilot 第三方插件仓库：STRM 媒体信息预热"], token=token)
        if create.returncode != 0 and "already exists" not in (create.stderr or ""):
            log(f"创建仓库失败：{create.stderr.strip()[:300]}")
            return False
        log(f"已创建仓库 {REPO}")
    else:
        log(f"仓库 {REPO} 已存在，直接推送")

    run(["git", "remote", "set-url", "origin", f"git@github.com:{REPO}.git"])
    push = run(["git", "push", "-u", "origin", "main"])
    if push.returncode != 0:
        log(f"SSH 推送失败：{push.stderr.strip()[:300]}，改用 HTTPS 重试")
        url = f"https://x-access-token:{token}@github.com/{REPO}.git"
        push = run(["git", "push", url, "main:main"])
        if push.returncode != 0:
            log(f"HTTPS 推送失败：{push.stderr.replace(token, '<token>').strip()[:300]}")
            return False
        run(["git", "remote", "set-url", "origin", f"git@github.com:{REPO}.git"])
    log(f"推送成功：https://github.com/{REPO}")
    return True


def main() -> int:
    """主循环：申请设备码、等待授权、发布仓库。"""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    for cycle in range(1, MAX_CYCLES + 1):
        try:
            info = request_device_code()
        except Exception as error:
            log(f"申请设备码失败：{error}")
            return 1
        log(f"第 {cycle} 轮 验证码={info['user_code']} 打开 {info['verification_uri']} 输入验证码授权"
            f"（{info['expires_in']} 秒内有效）")
        token = poll_token(info["device_code"], int(info.get("interval", 5)), int(info["expires_in"]))
        if not token:
            log("本轮未获授权，重新申请验证码")
            continue
        log("已获得访问令牌")
        if publish(token):
            log("发布流程完成")
            return 0
        log("发布失败，结束")
        return 1
    log("已达最大轮数，仍未获授权")
    return 1


if __name__ == "__main__":
    sys.exit(main())
