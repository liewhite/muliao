"""各领域共用的小值类型与格式：局部修改的「不改」标记、给人看的本地时间、校验错误的一行说明。"""

from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

from pydantic import ValidationError


class Keep(Enum):
    """局部修改里「不改这个字段」，用于本身可以设为 None 的字段（如取消指派、去掉前置检查）。"""

    KEEP = "keep"


KEEP = Keep.KEEP


def local_time(at: datetime, tz: ZoneInfo) -> str:
    """给人看的本地时间（精确到分钟），如 issue 时间线、触发器开的会话标题。"""
    return at.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def validation_reason(e: ValidationError) -> str:
    """pydantic 校验错误的一行说明（每处 `字段: 原因`，`;` 隔开），用在报给人或模型的问题里。"""
    return "; ".join(
        f"{'.'.join(str(x) for x in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()
    )
