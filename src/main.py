from __future__ import annotations

"""
E5Pulse - self-hosted Microsoft 365 E5 keep-alive service.
Copyright (C) 2026 TheMrxk

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.

E5Pulse 入口（开发文档 §7、§16）。

用法：
  python -m src.main                 # 长期运行（容器默认）
  python -m src.main --once          # 只跑一轮后退出（验证/手动触发）
  python -m src.main --config x.yaml # 指定配置文件
"""

import argparse
import asyncio
import logging
import signal

from .config import ConfigError, load_config
from .logger import setup_logging
from .scheduler import Scheduler

logger = logging.getLogger("e5pulse")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E5Pulse E5 订阅保活服务")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只执行一轮后退出")
    return parser.parse_args()


async def amain(config_path: str | None, once: bool) -> int:
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        # 日志系统还没起来，直接 stderr
        print(f"[startup failed] {exc}", flush=True)
        return 2

    setup_logging(config.global_.log_level, config.global_.log_file)

    enabled_accounts = [a for a in config.accounts if a.enabled]
    enabled_apis = sum(1 for api in config.default_apis if api.enabled)
    logger.info("=" * 60)
    logger.info("E5Pulse started | accounts %d (enabled %d) | default APIs %d (enabled %d)",
                len(config.accounts), len(enabled_accounts),
                len(config.default_apis), enabled_apis)
    for acc in config.accounts:
        logger.info("  账号[%s] auth=%s enabled=%s",
                    acc.name, acc.auth_type.value, acc.enabled)
    logger.info("=" * 60)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows 本地调试兜底
            signal.signal(sig, lambda *_: stop_event.set())

    scheduler = Scheduler(config, stop_event)

    if once:
        logger.info("--once 模式：执行单轮后退出")
        results = await scheduler.run_once()
        for name, result in results.items():
            logger.info("[%s] round result: %s%s",
                        name, "success" if result.success else "failure",
                        f"（{result.error}）" if result.error else "")
        return 0 if all(r.success for r in results.values()) else 1

    # 面板与调度循环同进程并存（§12.1 方案 A）
    panel_task = None
    server = None
    if config.panel.enabled:
        import uvicorn

        from .web.app import create_app
        # config_path 从加载器取实际路径（环境变量/CLI 可能覆盖默认值）
        from .config import DEFAULT_CONFIG_PATH
        import os
        config_path = config_path or os.environ.get(
            "E5PULSE_CONFIG", DEFAULT_CONFIG_PATH
        )
        web_app = create_app(config, scheduler, config_path)
        server = uvicorn.Server(uvicorn.Config(
            web_app, host=config.panel.host, port=config.panel.port,
            log_level="warning", access_log=False,
        ))
        panel_task = asyncio.create_task(server.serve())
        logger.info("面板已启动: http://%s:%d", config.panel.host, config.panel.port)

    try:
        await scheduler.run_forever()
    finally:
        if server is not None:
            server.should_exit = True
        if panel_task is not None:
            await asyncio.gather(panel_task, return_exceptions=True)
    logger.info("E5Pulse 已优雅退出")
    return 0


def main() -> None:
    args = _parse_args()
    try:
        exit_code = asyncio.run(amain(args.config, args.once))
    except KeyboardInterrupt:
        logger.info("收到 KeyboardInterrupt，退出")
        exit_code = 0
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
