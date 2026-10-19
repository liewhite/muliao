import os

import uvicorn

from muliao.api.app import create_app
from muliao.config import Settings
from muliao.container import build_container
from muliao.logging_setup import setup_logging


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    settings = Settings()  # type: ignore[call-arg]  必填项来自环境变量
    setup_logging(settings.log_level)
    app = create_app(build_container(settings), settings.web_dir)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
