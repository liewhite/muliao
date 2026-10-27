"""secret 的存放：条目（items，kind=secret），密文在 items.secret；key 的语法在这里（存储时校验，vault 的 ${vault:KEY} 引用
用同一个 KEY_SYNTAX）。加解密与引用的解析归 vault 领域（muliao.vault），这里只存取密文与描述、记版本：文档只有描述，
不含值，也不含任何由值推出的东西；改值时文档可能不变，照样记一版。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from muliao.agents.items import changeable_in, existing_in, usable_in, writable_in
from muliao.agents.kinds import StoredKind, Subject
from muliao.agents.revisions import record_in
from muliao.agents.tables import Item
from muliao.errors import InvalidInput
from muliao.platform.db import Database

# secret key 的语法。长度上限只在存储时检查：引用了超长 key 的模板照样认作引用，解析时按「不存在」报错，而不是当成字面
# 文本原样代入
KEY_SYNTAX = r"[A-Za-z_][A-Za-z0-9_]*"
_KEY_MAX = 100
_KEY = re.compile(rf"^{KEY_SYNTAX}$")


def validate_key(key: str) -> str:
    if not _KEY.fullmatch(key) or len(key) > _KEY_MAX:
        raise InvalidInput(
            f"invalid secret key {key!r}: must match {_KEY.pattern} and be at most {_KEY_MAX} characters"
        )
    return key


@dataclass(frozen=True)
class StoredSecret:
    owner: str | None
    key: str
    description: str
    updated_at: datetime
    ciphertext: bytes


def document(description: str) -> str:
    """版本表里的文档：只有描述。"""
    return json.dumps({"description": description}, ensure_ascii=False)


def _stored(row: Item) -> StoredSecret:
    assert row.secret is not None
    return StoredSecret(row.owner, row.name, row.description, row.updated_at, row.secret)


class SecretItems:
    def __init__(self, db: Database):
        self._db = db

    async def put(
        self, key: str, ciphertext: bytes, description: str, *, actor: str, owner: str | None
    ) -> None:
        """新建（归 owner）或改写（actor 要能改它，owner 不变）这个 secret 的值与描述。"""
        validate_key(key)
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

    async def delete(self, key: str, actor: str) -> None:
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.SECRET, key, actor)
            await s.delete(row)
            record_in(s, Subject.SECRET, key, document(row.description), None, actor, f"delete secret {key}")

    async def get(self, key: str) -> StoredSecret:
        async with self._db.transaction() as s:
            return _stored(await existing_in(s, StoredKind.SECRET, key))

    async def usable(self, user: str) -> list[StoredSecret]:
        """user（agent 的 id，或 USER 即全部）能用的（agents.access），按 key。"""
        async with self._db.transaction() as s:
            return [_stored(r) for r in await usable_in(s, StoredKind.SECRET, user)]
