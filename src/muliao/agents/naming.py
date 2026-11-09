import re
import unicodedata

from muliao.agents.ids import USER
from muliao.errors import InvalidInput

# skill 与 MCP server 名称：会出现在目录名和工具名前缀里（agent 用 id 标识，名称规则见 normalize_agent_name）
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


AGENT_NAME_MAX = 64


def normalize_agent_name(value: str) -> str:
    """agent 名称只用于展示（标识用 id），所以只要求：去首尾空白后非空、不超过 64 个字符、不含控制字符。
    NFC 规范化，使同一个字的不同编码形式被视为同一个名称（名称唯一）。"""
    name = unicodedata.normalize("NFC", value).strip()
    if not name or len(name) > AGENT_NAME_MAX:
        raise InvalidInput(f"agent name must be 1-{AGENT_NAME_MAX} characters, got {len(name)}")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise InvalidInput(f"agent name {value!r} must not contain control characters")
    return name


# 资源目录里平台的目录名（agents.assets.PLATFORM）：agent 不能取
PLATFORM_DIR = "platform"


def new_agent_name(value: str) -> str:
    """给 agent 起名或改名时的名称（读资源目录时目录名也按它查）：normalize_agent_name，不能是 USER——模型按名称指代 agent
    的地方（issue 的指派人等）用它指用户，同名的 agent 就分不出来了；它还是资源目录里的目录名（#498），所以要能当一个
    文件名：不含 `/`、`\\`，不以 `.` 开头（隐藏目录不读），不是 platform。只在起名时拦：按名称查找时照常规范化。"""
    name = normalize_agent_name(value)
    if name == USER:
        raise InvalidInput(f"agent name {USER!r} is reserved: it refers to the user (e.g. as issue assignee)")
    return file_name("agent", name)


def file_name(kind: str, name: str) -> str:
    """能当资源目录里一个文件或目录名的名字（agent 名、私有记忆挪到平台后的记忆名）。"""
    if "/" in name or "\\" in name or name.startswith(".") or name == PLATFORM_DIR:
        raise InvalidInput(
            f"{kind} name {name!r} must not contain '/' or '\\', start with '.', or be {PLATFORM_DIR!r}: it is a "
            "directory name in the asset tree"
        )
    return name


def validate_slug(kind: str, value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise InvalidInput(f"invalid {kind} name {value!r}: must match {_SLUG.pattern}")
    return value
