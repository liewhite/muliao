import argparse
import os
from pathlib import Path

import uvicorn

from muliao.api.app import create_app
from muliao.config import DEFAULT_CONFIG_PATH, WEB_DIR, load_settings
from muliao.container import assemble
from muliao.logging_setup import setup_logging

# 关停时等 HTTP 连接自己结束的时长；进程管理器给的退出时限（launchd 的 ExitTimeOut）要大于它加上 lifespan
# 关停的时长（后台任务清理最多约 20 秒），见 docs/deployment.md
GRACEFUL_SHUTDOWN_SECONDS = 3


def main() -> None:
    parser = argparse.ArgumentParser(prog="muliao", description="Personal multi-agent assistant")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config file (default: {DEFAULT_CONFIG_PATH}); template: config.example.toml",
    )
    args = parser.parse_args()
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    settings = load_settings(args.config)
    setup_logging(settings.log_level)
    app = create_app(
        assemble(settings).container, WEB_DIR, auth=settings.auth, public_url=settings.server.public_url
    )
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
        proxy_headers=True,
        # 界面的 SSE 长连接不会自己关：不设上限时 uvicorn 一直等它们，lifespan 的关停（取消运行、结束后台任务的
        # 进程组并通知 agent）根本轮不到，进程管理器超时后强杀。到时取消剩下的连接，进入 lifespan 关停。
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )


if __name__ == "__main__":
    main()
