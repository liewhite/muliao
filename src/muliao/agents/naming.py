import re
import unicodedata

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


def validate_slug(kind: str, value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise InvalidInput(f"invalid {kind} name {value!r}: must match {_SLUG.pattern}")
    return value
