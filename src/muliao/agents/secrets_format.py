"""secret 文件 `secrets/<KEY>.toml` 的格式与 key 的语法（agents.assets 读，agents.secrets 写）：

```toml
description = "…"
ciphertext = "gAAAA…"   # Fernet 密文（agents.secrets.Vault 加密），不能手写
```

key 语法也用于 `${vault:KEY}` 引用（VAULT_REF）。
"""

from __future__ import annotations

import re
import tomllib

import tomlkit

from muliao.errors import InvalidInput

# secret key 的语法。长度上限只在存储时检查：引用了超长 key 的模板照样认作引用，解析时按「不存在」报错，而不是当成字面
# 文本原样代入
KEY_SYNTAX = r"[A-Za-z_][A-Za-z0-9_]*"
_KEY_MAX = 100
_KEY = re.compile(rf"^{KEY_SYNTAX}$")

# 配置模板中的引用语法：${vault:KEY}
VAULT_REF = re.compile(rf"\$\{{vault:({KEY_SYNTAX})\}}")


def validate_key(key: str) -> str:
    if not _KEY.fullmatch(key) or len(key) > _KEY_MAX:
        raise InvalidInput(
            f"invalid secret key {key!r}: must match {_KEY.pattern} and be at most {_KEY_MAX} characters"
        )
    return key


def parse_secret_file(key: str, text: str) -> tuple[str, bytes]:
    """返回（描述, 密文）；不合规则时 InvalidInput。密文能不能解开在用到时才知道（解不开报出是哪个 key）。"""
    validate_key(key)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise InvalidInput(f"cannot parse: {e}") from e
    extra = sorted(set(data) - {"description", "ciphertext"})
    description, ciphertext = data.get("description", ""), data.get("ciphertext")
    if extra or not isinstance(description, str) or not isinstance(ciphertext, str) or not ciphertext:
        raise InvalidInput(
            "a secret file has only 'description' (string) and 'ciphertext' (set by vault_set; never write it by hand)"
        )
    return description, ciphertext.encode()


def secret_file(description: str, ciphertext: bytes) -> str:
    doc = tomlkit.document()
    doc["description"] = description
    doc["ciphertext"] = ciphertext.decode()
    return tomlkit.dumps(doc)
