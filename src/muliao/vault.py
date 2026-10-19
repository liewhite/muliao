"""加密保存的 secret。agent 只能通过 key 引用，没有读取明文的工具。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.errors import InvalidInput, NotFound
from muliao.models import Secret

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
# 配置模板中的引用语法：${vault:KEY}
VAULT_REF = re.compile(r"\$\{vault:([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class SecretInfo:
    key: str
    description: str
    updated_at: datetime


def validate_key(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise InvalidInput(f"invalid secret key {key!r}: must match {_KEY.pattern}")
    return key


class Vault:
    def __init__(self, db: Database, fernet_key: str):
        self._db = db
        self._fernet = Fernet(fernet_key.encode())

    async def set(self, key: str, value: str, description: str = "") -> None:
        validate_key(key)
        if not value:
            raise InvalidInput("secret value must not be empty")
        async with self._db.transaction() as s:
            row = await s.get(Secret, key)
            ciphertext = self._fernet.encrypt(value.encode())
            if row is None:
                s.add(Secret(key=key, ciphertext=ciphertext, description=description))
            else:
                row.ciphertext = ciphertext
                row.description = description

    async def delete(self, key: str) -> None:
        async with self._db.transaction() as s:
            row = await s.get(Secret, key)
            if row is None:
                raise NotFound(f"secret {key!r} not found")
            await s.delete(row)

    async def list(self) -> list[SecretInfo]:
        async with self._db.transaction() as s:
            rows = (await s.scalars(select(Secret).order_by(Secret.key))).all()
            return [SecretInfo(r.key, r.description, r.updated_at) for r in rows]

    async def resolve_many(self, keys: list[str]) -> dict[str, str]:
        async with self._db.transaction() as s:
            return {k: await self._resolve(s, k) for k in keys}

    async def _resolve(self, s: AsyncSession, key: str) -> str:
        row = await s.get(Secret, key)
        if row is None:
            raise NotFound(f"secret {key!r} not found in vault")
        return self._fernet.decrypt(row.ciphertext).decode()

    async def render(self, template: str) -> str:
        """把字符串中的 ${vault:KEY} 替换为明文；引用不存在的 key 抛 NotFound。"""
        keys = list(dict.fromkeys(VAULT_REF.findall(template)))
        if not keys:
            return template
        values = await self.resolve_many(keys)
        return VAULT_REF.sub(lambda m: values[m.group(1)], template)

    async def render_map(self, mapping: dict[str, str]) -> dict[str, str]:
        return {k: await self.render(v) for k, v in mapping.items()}
