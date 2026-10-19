import re

from muliao.errors import InvalidInput

# agent、skill、MCP server 名称共用：会出现在目录名和工具名前缀里
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_slug(kind: str, value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise InvalidInput(f"invalid {kind} name {value!r}: must match {_SLUG.pattern}")
    return value
