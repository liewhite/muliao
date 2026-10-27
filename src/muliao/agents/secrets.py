"""secret：条目（items，kind=secret），密文在 items.secret（Fernet 加密）。agent 只能通过 key 引用，没有读取明文的工具。

这里管 key 的语法（存储时校验；${vault:KEY} 引用用同一个 KEY_SYNTAX）、加解密、存取与记版本，以及 ${vault:KEY} 引用
的解析。密文不出这个模块：对外只有不含值的 SecretInfo，与按使用者解析出的明文（resolve_many）。版本表里的文档只有
描述，不含值，也不含任何由值推出的东西；改值时文档可能不变，照样记一版。

引用按使用者能用的 secret 解析（agents.access 的规则 1：自己的与分发给它的）：agent 运行的命令与它连接的 MCP server
都用该 agent；用户（USER，如界面上新增 MCP 时的探测）能用全部。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet

from muliao.agents.access import changeable_in, usable_in, writable_in
from muliao.agents.items import existing_in
from muliao.agents.kinds import StoredKind, Subject
from muliao.agents.revisions import record_in
from muliao.agents.tables import Item
from muliao.errors import InvalidInput, NotFound
from muliao.platform.db import Database

log = logging.getLogger(__name__)

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


def fill(template: str, values: Mapping[str, str]) -> str:
    """把字符串中的 ${vault:KEY} 替换为 values 里的明文（values 须含全部引用，通常来自 resolve_many）。"""
    return VAULT_REF.sub(lambda m: values[m.group(1)], template)


def document(description: str) -> str:
    """版本表里的文档：只有描述。"""
    return json.dumps({"description": description}, ensure_ascii=False)


@dataclass(frozen=True)
class SecretInfo:
    """一个 secret 的信息，不含值。"""

    owner: str | None
    key: str
    description: str
    updated_at: datetime


def _info(row: Item) -> SecretInfo:
    return SecretInfo(row.owner, row.name, row.description, row.updated_at)


def _ciphertext(secret: bytes | None) -> bytes:
    assert secret is not None, "a secret item always has its ciphertext"
    return secret


class Vault:
    def __init__(self, db: Database, fernet_key: str):
        self._db = db
        self._fernet = Fernet(fernet_key.encode())

    async def set(self, key: str, value: str, description: str, *, actor: str, owner: str | None) -> None:
        """新建（归 owner）或改写（actor 要能改它，owner 不变）这个 secret 的值与描述。"""
        if not value:
            raise InvalidInput("secret value must not be empty")
        validate_key(key)
        ciphertext = self._fernet.encrypt(value.encode())
        async with self._db.transaction() as s:
            row = await writable_in(s, StoredKind.SECRET, key, actor, owner)
            before = None if row is None else document(row.description)
            if row is None:
                row = Item(kind=StoredKind.SECRET.value, name=key, owner=owner)
                s.add(row)
            row.secret, row.description = ciphertext, description
            # 每次都设了值：文档（只有描述）不变也记一版
            record_in(
                s, Subject.SECRET, key, before, document(description), actor, f"set secret {key}", force=True
            )
        # 别的 agent 可能依赖它：记录谁改了哪个 key（不记值）
        log.info("secret set", extra={"owner": owner, "key": key, "actor": actor})

    async def delete(self, key: str, actor: str) -> None:
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.SECRET, key, actor)
            await s.delete(row)
            record_in(s, Subject.SECRET, key, document(row.description), None, actor, f"delete secret {key}")
        log.info("secret deleted", extra={"key": key, "actor": actor})

    async def get(self, key: str) -> SecretInfo:
        """一个 secret 的信息（不含值）。"""
        async with self._db.transaction() as s:
            return _info(await existing_in(s, StoredKind.SECRET, key))

    async def usable_by(self, user: str) -> list[SecretInfo]:
        """user（agent 的 id，或 USER 即全部）能用的 secret（agents.access，不含值），按 key。"""
        async with self._db.transaction() as s:
            return [_info(r) for r in await usable_in(s, StoredKind.SECRET, user)]

    async def resolve_many(self, user: str, keys: list[str]) -> dict[str, str]:
        """取 user（agent 的 id，或 USER）能用的 secret 的明文；任一 key 不能用即报错。"""
        if not keys:
            return {}
        async with self._db.transaction() as s:
            rows = {r.name: r.secret for r in await usable_in(s, StoredKind.SECRET, user)}
        missing = [k for k in keys if k not in rows]
        if missing:
            raise NotFound(
                f"secret(s) {missing} not found among the secrets you can use (your own and those given to you "
                "by your roles or your own allow rules)"
            )
        return {k: self._fernet.decrypt(_ciphertext(rows[k])).decode() for k in keys}
