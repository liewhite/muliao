import logging

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


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
