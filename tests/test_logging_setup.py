import logging
import subprocess
import sys
import textwrap

import pytest

from muliao.logging_setup import _DropPolledAccess


def _access(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:0", method, path, "1.1", status),
        None,
    )


@pytest.mark.parametrize(
    ("method", "path", "status", "kept"),
    [
        ("GET", "/api/notifications/unread", 200, False),
        ("GET", "/api/notifications/unread?x=1", 304, False),
        ("GET", "/api/notifications/unread", 401, True),  # 失败照记
        ("GET", "/api/notifications", 200, True),  # 只滤轮询接口
        ("POST", "/api/notifications/unread", 200, True),
    ],
)
def test_successful_polling_access_lines_are_dropped(method, path, status, kept):
    assert _DropPolledAccess().filter(_access(method, path, status)) is kept


def test_log_file_rotates_and_takes_stdout_and_stderr_along(tmp_path):
    # 重定向 fd 1、2 会改掉 pytest 自己的输出，所以在子进程里跑
    log = tmp_path / "logs" / "muliao.log"
    script = textwrap.dedent(f"""
        import logging, os, sys
        from pathlib import Path
        from muliao.logging_setup import setup_logging

        setup_logging("INFO", Path({str(log)!r}), max_bytes=300, backups=2)
        print("printed before rotation", flush=True)
        for i in range(20):
            logging.getLogger("t").info("line %02d %s", i, "x" * 40)
        print("printed after rotation", flush=True)
        os.write(2, b"raw stderr\\n")
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    assert done.returncode == 0, done.stderr
    assert done.stdout == done.stderr == ""  # 全进了日志文件
    files = sorted(p.name for p in log.parent.iterdir())
    assert files == ["muliao.log", "muliao.log.1", "muliao.log.2"]  # 只留 2 份旧文件
    current = log.read_text()
    assert "line 19" in current
    assert (
        "printed after rotation" in current and "raw stderr" in current
    )  # 进的是当前文件，不是改名后的旧文件
    assert "printed before rotation" not in current


def test_a_log_file_without_rotation_limits_is_rejected(tmp_path):
    from muliao.logging_setup import setup_logging

    with pytest.raises(ValueError, match="positive"):
        setup_logging("INFO", tmp_path / "x.log", max_bytes=0, backups=4)
