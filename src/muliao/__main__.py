import argparse
import os
import socket
import sys
from pathlib import Path

import uvicorn
from uvicorn.main import STARTUP_FAILURE

from muliao.api.app import create_app
from muliao.config import DEFAULT_CONFIG_PATH, WEB_DIR, load_settings
from muliao.container import assemble
from muliao.logging_setup import setup_logging
from muliao.platform.bus import EventBus

# 关停时等 HTTP 连接自己结束的时长；进程管理器给的退出时限（launchd 的 ExitTimeOut）要大于它加上 lifespan
# 关停的时长，见 docs/deployment.md
GRACEFUL_SHUTDOWN_SECONDS = 3


class _Server(uvicorn.Server):
    """关停时先关事件总线：界面的 SSE 长连接不会自己关，总线结束订阅后它们在排空期内正常结束（浏览器自动重连），
    uvicorn 不必到时强行取消它们并打 ERROR（#330）。"""

    def __init__(self, config: uvicorn.Config, bus: EventBus):
        super().__init__(config)
        self._bus = bus

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._bus.close()
        await super().shutdown(sockets)


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
    assembly = assemble(settings)
    app = create_app(assembly.container, WEB_DIR, auth=settings.auth, public_url=settings.server.public_url)
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,
        proxy_headers=True,
        # SSE 由 _Server 关总线结束；其余还没结束的请求（如正在上传、下载的）到时取消，进入 lifespan 的关停
        # （取消运行、结束后台任务的进程组并通知 agent），不让它们无限拖住
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = _Server(config, assembly.bus)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    # 与 uvicorn.run 一致：没启动起来（如端口被占）以非零码退出
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
