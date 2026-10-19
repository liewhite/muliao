import re
import unicodedata

from muliao.errors import InvalidInput

# agent、skill、MCP server 名称共用：会出现在目录名和工具名前缀里
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# agent 名称：允许中文等 Unicode 字母数字，用作目录名、URL 路径和主键
_AGENT_NAME = re.compile(r"^\w[\w-]{0,63}$")


def normalize_agent_name(value: str) -> str:
    """校验并返回 NFC 规范形式。

    - 只允许 Unicode 字母、数字、`_`、`-`，首字符不能是 `-`：不含路径分隔符和 `.`，杜绝路径穿越；
    - NFC 规范化：同一个字的不同编码形式只对应一个 agent；
    - 禁止大写：macOS 文件系统默认不区分大小写，`Alpha` 与 `alpha` 会落到同一目录。
    """
    name = unicodedata.normalize("NFC", value)
    if not _AGENT_NAME.fullmatch(name):
        raise InvalidInput(
            f"invalid agent name {value!r}: use letters (Chinese allowed), digits, '_' or '-', "
            "not starting with '-', at most 64 characters"
        )
    if name != name.lower():
        raise InvalidInput(f"invalid agent name {value!r}: uppercase letters are not allowed")
    return name


def validate_slug(kind: str, value: str) -> str:
    if not _SLUG.fullmatch(value):
        raise InvalidInput(f"invalid {kind} name {value!r}: must match {_SLUG.pattern}")
    return value
