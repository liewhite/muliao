"""各领域共用的小值类型与格式：局部修改的「不改」标记、给人看的本地时间。"""

from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo


class Keep(Enum):
    """局部修改里「不改这个字段」，用于本身可以设为 None 的字段（如取消指派、去掉前置检查）。"""

    KEEP = "keep"


KEEP = Keep.KEEP


def local_time(at: datetime, tz: ZoneInfo) -> str:
    """给人看的本地时间（精确到分钟），如 issue 时间线、触发器开的会话标题。"""
    return at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
