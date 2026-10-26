"""secret 的存放：条目（items，kind=secret），密文在 items.secret。加解密、key 的语法与引用 ${vault:KEY} 的解析归 vault 领域
（muliao.vault），这里只存取密文与描述、记版本（文档只有描述，不含值，也不含任何由值推出的东西）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from muliao.agents.items import items_in, owned_item_in, writable_in
from muliao.agents.ownership import owned_by, scope_of, visible_owners
from muliao.agents.revisions import Subject, record_in
from muliao.agents.tables import Item
from muliao.platform.db import Database, utcnow


@dataclass(frozen=True)
class StoredSecret:
    owner: str | None
    key: str
    description: str
    updated_at: datetime
    ciphertext: bytes


def document(description: str, value_set_at: datetime) -> str:
    """版本表里的文档：描述与值最后一次设置的时间（只改值时前后也不同，照样记一版）。"""
    return json.dumps({"description": description, "value_set_at": value_set_at.isoformat()})


def _stored(row: Item) -> StoredSecret:
    assert row.secret is not None
    return StoredSecret(row.owner, row.name, row.description, row.updated_at, row.secret)


class SecretItems:
    def __init__(self, db: Database):
        self._db = db

    async def put(
        self, owner: str | None, key: str, ciphertext: bytes, description: str, author: str
    ) -> None:
        """新建或改写 owner 名下的这个 secret（别人名下有同名的即冲突）。"""
        async with self._db.transaction() as s:
            row = await writable_in(s, Subject.SECRET, owner, key)
            before = None if row is None else document(row.description, row.updated_at)
            now = utcnow()
            if row is None:
                row = Item(kind=Subject.SECRET.value, name=key, owner=owner)
                s.add(row)
            row.secret, row.description, row.updated_at = ciphertext, description, now
            record_in(
                s,
                Subject.SECRET,
                key,
                before,
                document(description, now),
                author,
                f"set {scope_of(owner)} secret {key}",
            )

    async def delete(self, owner: str | None, key: str, author: str) -> None:
        async with self._db.transaction() as s:
            row = await owned_item_in(s, Subject.SECRET, owner, key, write=True)
            await s.delete(row)
            record_in(
                s,
                Subject.SECRET,
                key,
                document(row.description, row.updated_at),
                None,
                author,
                f"delete {scope_of(owner)} secret {key}",
            )

    async def owned(self, owner: str | None) -> list[StoredSecret]:
        """owner 名下的（None 为公共），按 key。"""
        async with self._db.transaction() as s:
            return [_stored(r) for r in await items_in(s, Subject.SECRET, owned_by(Item.owner, owner))]

    async def visible(self, agent_id: str | None) -> list[StoredSecret]:
        """agent_id 看得到的（ownership.sees；None 为只看公共）：私有在前，各按 key。"""
        async with self._db.transaction() as s:
            return [
                _stored(r) for r in await items_in(s, Subject.SECRET, visible_owners(Item.owner, agent_id))
            ]
