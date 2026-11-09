"""真机验证：真实启动 uvicorn 面板，确认端口监听与完整 HTTP 鉴权链路。"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_panel_server(tmp_path: Path) -> None:
    port = free_port()
    cfg = {
        "global": {
            "log_file": str(tmp_path / "logs" / "e5.log"),
            "state_file": str(tmp_path / "status.json"),
            # 巡检间隔拉大，测试期间不触发真实调度
        },
        "schedule": {"enabled": False},
        "default_apis": [{
            "name": "get_users",
            "url": "https://graph.microsoft.com/v1.0/users?$top=5",
            "enabled": True,
        }],
        "panel": {
            "enabled": True, "host": "127.0.0.1", "port": port,
            "password": "live-pass-1234567890", "login_fail_limit": 3,
        },
        "accounts": [{
            "name": "live", "enabled": False,  # 全禁用会被模型拒，故下面再开一个
            "auth_type": "client_credentials",
            "tenant_id": "11111111-2222-3333-4444-555555555555",
            "client_id": "c1", "client_secret": "s-1234567890",
        }, {
            "name": "live2", "enabled": True,
            "auth_type": "client_credentials",
            "tenant_id": "11111111-2222-3333-4444-555555555555",
            "client_id": "c2", "client_secret": "s-1234567890",
        }],
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "src.main", "--config", str(cfg_path)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # 等待服务起来（轮询 health，最多 ~10s）
        ok = False
        for _ in range(50):
            try:
                if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                    ok = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        assert ok, "面板服务未在预期时间内启动"

        # 未登录 401
        assert httpx.get(f"{base}/api/status", timeout=2).status_code == 401
        # 错误密码 401
        assert httpx.post(f"{base}/api/auth/login",
                          json={"password": "wrong"}, timeout=2).status_code == 401
        # 正确密码登录
        login = httpx.post(f"{base}/api/auth/login",
                           json={"password": "live-pass-1234567890"}, timeout=2)
        assert login.status_code == 200
        cookie = login.cookies
        # 带 cookie 访问
        st = httpx.get(f"{base}/api/status", cookies=cookie, timeout=2)
        assert st.status_code == 200
        names = {a["name"] for a in st.json()["accounts"]}
        assert names == {"live", "live2"}
        # secret 不泄露
        assert "s-1234567890" not in st.text
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        out = proc.stdout.read() if proc.stdout else ""
        assert proc.returncode == 0 or proc.returncode == -15, f"异常退出:\n{out}"
