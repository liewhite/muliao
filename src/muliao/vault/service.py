"""加密保存的 secret，分公共与 agent 私有。agent 只能通过 key 引用，没有读取明文的工具。

引用 ${vault:KEY} 按使用者的视角解析（合并规则见 ownership.shadowing）：agent 运行的命令用该 agent 的视角，
MCP server 的配置用 server 归属者的视角——公共 server 只能引用公共 secret。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import active_agent_in, owned_by, scope_of, shadowing, visible, visible_owners
from muliao.agents.events import AgentDeleting
from muliao.errors import InvalidInput, NotFound
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit
from muliao.vault.tables import Secret

log = logging.getLogger(__name__)

# secret key 的语法：存储时校验与模板里的引用共用这一份
_KEY_SYNTAX = r"[A-Za-z_][A-Za-z0-9_]{0,99}"
_KEY = re.compile(rf"^{_KEY_SYNTAX}$")
# 配置模板中的引用语法：${vault:KEY}
VAULT_REF = re.compile(rf"\$\{{vault:({_KEY_SYNTAX})\}}")


def fill(template: str, values: Mapping[str, str]) -> str:
    """把字符串中的 ${vault:KEY} 替换为 values 里的明文（values 须含全部引用，通常来自 resolve_many）。"""
    return VAULT_REF.sub(lambda m: values[m.group(1)], template)


@dataclass(frozen=True)
class SecretInfo:
    owner: str | None
    key: str
    description: str
    updated_at: datetime


def validate_key(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise InvalidInput(f"invalid secret key {key!r}: must match {_KEY.pattern}")
    return key


def _info(row: Secret) -> SecretInfo:
    return SecretInfo(row.owner, row.key, row.description, row.updated_at)


class Vault:
    def __init__(self, db: Database, fernet_key: str):
        self._db = db
        self._fernet = Fernet(fernet_key.encode())

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "vault.agent_deleting", self.on_agent_deleting)

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：删除它的私有 secret。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(delete(Secret).where(Secret.owner == event.agent_id))

        return commit

    async def set(self, owner: str | None, key: str, value: str, description: str = "") -> None:
        validate_key(key)
        if not value:
            raise InvalidInput("secret value must not be empty")
        ciphertext = self._fernet.encrypt(value.encode())
        async with self._db.transaction() as s:
            if owner is not None:
                # 正在删除的 agent 按不存在处理：否则删除流程清理过之后又写进来的 secret 没人认领
                await active_agent_in(s, owner)
            row = await s.scalar(select(Secret).where(owned_by(Secret.owner, owner), Secret.key == key))
            if row is None:
                s.add(Secret(owner=owner, key=key, ciphertext=ciphertext, description=description))
            else:
                row.ciphertext = ciphertext
                row.description = description
        # 公共 secret 所有 agent 都依赖：记录谁在什么作用域改了哪个 key（不记值）
        log.info("secret set", extra={"owner": owner, "key": key})

    async def delete(self, owner: str | None, key: str) -> None:
        async with self._db.transaction() as s:
            row = await s.scalar(select(Secret).where(owned_by(Secret.owner, owner), Secret.key == key))
            if row is None:
                raise NotFound(f"{scope_of(owner)} secret {key!r} not found")
            await s.delete(row)
        log.info("secret deleted", extra={"owner": owner, "key": key})

    async def view(self, owner: str | None) -> list[tuple[SecretInfo, bool]]:
        """管理界面：owner 视角下的 secret（不含值），即它私有的与公共的，各自标明是否被同名私有项遮蔽。

        owner 为 None 时只有公共 secret。顺序：私有在前，各按 key 排序。
        """
        async with self._db.transaction() as s:
            query = select(Secret).where(visible_owners(Secret.owner, owner))
            rows = (await s.scalars(query.order_by(Secret.owner.is_(None), Secret.key))).all()
            view = shadowing(rows, agent_id=owner, owner=lambda r: r.owner, name=lambda r: r.key)
            return [(_info(r), shadowed) for r, shadowed in view]

    async def visible_to(self, agent_id: str | None) -> list[SecretInfo]:
        async with self._db.transaction() as s:
            return [_info(r) for r in await self._visible_rows(s, agent_id)]

    async def _visible_rows(self, s: AsyncSession, agent_id: str | None) -> list[Secret]:
        query = select(Secret).order_by(Secret.key)
        query = query.where(visible_owners(Secret.owner, agent_id))
        rows = (await s.scalars(query)).all()
        return visible(rows, agent_id=agent_id, owner=lambda r: r.owner, name=lambda r: r.key)

    async def resolve_many(self, agent_id: str | None, keys: list[str]) -> dict[str, str]:
        """按 agent_id 的视角取明文（None 为只看公共）；任一 key 不可见即报错。"""
        if not keys:
            return {}
        async with self._db.transaction() as s:
            rows = {r.key: r for r in await self._visible_rows(s, agent_id)}
        missing = [k for k in keys if k not in rows]
        if missing:
            where = "public secrets" if agent_id is None else "private or public secrets"
            raise NotFound(f"secret(s) {missing} not found in {where}")
        return {k: self._fernet.decrypt(rows[k].ciphertext).decode() for k in keys}
