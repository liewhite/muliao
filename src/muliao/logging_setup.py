import logging
import logging.handlers
import os
import sys
from pathlib import Path

# LogRecord 自带的属性；其余属性来自 `extra=`，作为结构化字段输出
_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        if not extras:
            return base
        fields = " ".join(f"{k}={v!r}" for k, v in extras.items())
        head, sep, tail = base.partition("\n")
        return f"{head} | {fields}{sep}{tail}"


# HTTP 客户端每个请求一条 INFO（「HTTP Request: GET <完整 URL>」）：URL 里可能带着代入的 secret（如 MCP server
# 地址里的 ?token=），量也大。只留 WARNING 以上
_HTTP_CLIENT_LOGGERS = ("httpx", "httpx2")


def quiet_http_clients() -> None:
    for name in _HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# 前端每隔几秒轮询这些接口，成功的访问日志一周上万行、没有排查价值；失败（4xx/5xx）照记
_POLLED_PATHS = ("/api/notifications/unread",)


class _DropPolledAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access 的 args：(client_addr, method, full_path, http_version, status_code)
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _, method, full_path, _, status = args
        path = str(full_path).partition("?")[0]
        return not (method == "GET" and path in _POLLED_PATHS and isinstance(status, int) and status < 400)


class _StdioRotatingHandler(logging.handlers.RotatingFileHandler):
    """按大小轮转的日志文件，并把进程的 stdout/stderr（fd 1、2）也指到当前文件。

    launchd 的 StandardOutPath 只在启动时打开一次，外部工具（newsyslog）改名轮转后它仍写旧文件；所以由进程自己
    轮转，每次换新文件后把 fd 1、2 重定向过去，未捕获的异常、库直接 print 的内容也进当前文件。
    """

    def __init__(self, path: Path, max_bytes: int, backups: int):
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        self._redirect_stdio()

    def doRollover(self) -> None:
        super().doRollover()
        self._redirect_stdio()

    def _redirect_stdio(self) -> None:
        if self.stream is None:
            return
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        fd = self.stream.fileno()
        os.dup2(fd, 1)
        os.dup2(fd, 2)


def setup_logging(level: str, file: Path | None = None, *, max_bytes: int = 0, backups: int = 0) -> None:
    handler = logging.StreamHandler() if file is None else _StdioRotatingHandler(file, max_bytes, backups)
    handler.setFormatter(KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    access = logging.getLogger("uvicorn.access")
    access.filters[:] = [f for f in access.filters if not isinstance(f, _DropPolledAccess)] + [
        _DropPolledAccess()
    ]
    quiet_http_clients()
