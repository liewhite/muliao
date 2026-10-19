import argparse
import os
from pathlib import Path

import uvicorn

from muliao.api.app import create_app
from muliao.config import DEFAULT_CONFIG_PATH, WEB_DIR, load_settings
from muliao.container import build_container
from muliao.logging_setup import setup_logging


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
    app = create_app(build_container(settings), WEB_DIR)
    uvicorn.run(
        app, host=settings.server.host, port=settings.server.port, log_config=None, proxy_headers=True
    )


if __name__ == "__main__":
    main()
