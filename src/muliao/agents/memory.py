"""记忆：AI 资产的一种（items.kind = memory），一块有长度上限的 markdown。agent 能用（use）的每一块，每次运行全文放进
instructions；能改（write）的才能用 memory_edit 改。谁能用、能改是分发子域的规则（agents.access），每次修改记一版
（revisions，subject memory，name 是记忆名，文档是正文）。

三类记忆，规则相同，只是分发不同：
- 全局记忆（GLOBAL_MEMORY = global）：归平台，base 固定收录（use）；admin 靠 `write:*` 能改。迁移时建好，不能删。
- 共享记忆（如 trading）：归平台，由 admin 建（memory_create），收录它的角色给 `*:memory:<名>`，成员都能用也都能改。
- 私有记忆：建 agent 时与它的行同一事务建一块（create_memory_in），归这个 agent，名字取当时的 agent 名（改名不跟着改）；
  删 agent 时与它名下的别的资产一样处理（agents.ownership.retire_owned_in：分发出去的转给平台，其余删掉）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.access import changeable_in, grants_in, usable_in, writable_in
from muliao.agents.items import creatable_in, existing_in, item_in
from muliao.agents.kinds import Kind, StoredKind, Subject, label
from muliao.agents.naming import validate_slug
from muliao.agents.permissions import GLOBAL_MEMORY
from muliao.agents.revisions import described, record_in, revision_doc_in
from muliao.agents.tables import Item
from muliao.errors import Conflict, InvalidInput
from muliao.platform import text_edit
from muliao.platform.db import Database

_KIND = StoredKind.MEMORY
_LABEL = label(_KIND)


class MemoryFull(InvalidInput):
    def __init__(self, message: str, current: str):
        super().__init__(message)
        self.current = current  # 当时的记忆正文：memory_edit 的报错附上它


@dataclass(frozen=True)
class MemoryBlock:
    """一次运行放进 instructions 的一块记忆。"""

    name: str
    content: str
    # 这个 agent 能改它（规则 1 的 write）
    editable: bool
    # 归这个 agent（它的私有记忆）
    yours: bool


@dataclass(frozen=True)
class MemoryInfo:
    name: str
    owner: str | None
    description: str
    content: str
    updated_at: datetime


def _info(row: Item) -> MemoryInfo:
    return MemoryInfo(row.name, row.owner, row.description, row.content, row.updated_at)


def create_memory_in(s: AsyncSession, agent_id: str, name: str, actor: str) -> None:
    """新建 agent 的私有记忆（空，名字是 agent 名），在插入它的行的同一事务里（agent 领域内部，AgentService.create）。
    同名的记忆已存在时由调用方先用 check_memory_name_free_in 拦下。"""
    s.add(Item(kind=_KIND.value, name=name, owner=agent_id, content=""))
    record_in(s, Subject.MEMORY, name, None, "", actor, "create memory")


async def check_memory_name_free_in(s: AsyncSession, name: str) -> None:
    """新 agent 的私有记忆取它的名字：同名的记忆已经有了（如别的 agent 改名前用过这个名字）就不能用这个名字建 agent。"""
    if await item_in(s, _KIND, name) is not None:
        raise Conflict(
            f"a memory named {name!r} already exists (agents' private memories keep the name they were created "
            "with); choose another agent name"
        )


class MemoryStore:
    def __init__(self, db: Database, max_chars: int):
        self._db = db
        # 每一块各自的上限
        self.max_chars = max_chars

    async def read(self, name: str) -> MemoryInfo:
        async with self._db.transaction() as s:
            return _info(await existing_in(s, _KIND, name))

    async def for_run(self, agent_id: str) -> list[MemoryBlock]:
        """组装一次运行的指令时读：这个 agent 能用的每一块（自己的与规则分来的），按名字。包括正在删除的 agent 的：删除
        标记之后仍可能有它的运行开始执行，由删除流程停下，不该因为读不到记忆而失败（与 sessions.loading 读 agent 同一
        口径）。"""
        async with self._db.transaction() as s:
            grants = await grants_in(s, agent_id)
            return [
                MemoryBlock(
                    r.name,
                    r.content,
                    grants.writes(Kind.MEMORY, r.name, r.owner),
                    r.owner == agent_id,
                )
                for r in await usable_in(s, _KIND, agent_id)
            ]

    async def create(
        self,
        name: str,
        content: str,
        *,
        actor: str,
        owner: str | None = None,
        description: str = "",
        reason: str | None = None,
    ) -> MemoryInfo:
        """只新建一块记忆（共享记忆归平台：owner 为 None），同名的已存在 Conflict。分给谁靠角色收录它（`*:memory:<名>`）。"""
        async with self._db.transaction() as s:
            await creatable_in(s, _KIND, name, owner)
            return await self._new_in(
                s, name, content, owner, description, actor, described(reason, f"create memory {name}")
            )

    async def put(
        self, name: str, content: str, description: str, *, actor: str, owner: str | None = None
    ) -> MemoryInfo:
        """新建或整体改写（用户在界面上编辑）：已有的要 actor 能改（规则 1 的 write），owner 不变；新建的归 owner。"""
        async with self._db.transaction() as s:
            row = await writable_in(s, _KIND, name, actor, owner)
            if row is None:
                return await self._new_in(
                    s, name, content, owner, description, actor, f"create memory {name}"
                )
            self._check_size(len(content), name)
            record_in(s, Subject.MEMORY, name, row.content, content, actor, f"write memory {name}")
            row.content, row.description = content, description
            await s.flush()
            return _info(row)

    async def _new_in(
        self,
        s: AsyncSession,
        name: str,
        content: str,
        owner: str | None,
        description: str,
        actor: str,
        message: str,
    ) -> MemoryInfo:
        validate_slug(_LABEL, name)
        self._check_size(len(content), name)
        row = Item(kind=_KIND.value, name=name, owner=owner, content=content, description=description)
        s.add(row)
        record_in(s, Subject.MEMORY, name, None, content, actor, message)
        await s.flush()
        return _info(row)

    async def edit(
        self, name: str, old: str, new: str, actor: str, *, reason: str | None = None
    ) -> MemoryInfo:
        """规则同 skill 正文（text_edit.edit）：old 恰好出现一次时替换为 new（new 为空即删除），old 为空时把 new 追加到末尾。
        actor 要能改它（规则 1 的 write）。reason 是这一版的说明（revisions.described）。

        原文对不上或改后超长时，报错附上当前全文：指令里的记忆是运行开始时的快照，别的会话改过之后，模型没有别的办法
        读到最新的内容（#349）。"""

        async def apply(row: Item, _s: AsyncSession) -> str:
            try:
                return text_edit.edit(row.content, old, new, f"memory {name}")
            except InvalidInput as e:
                if not old:
                    raise  # 追加空内容：与原文无关
                raise InvalidInput(f"{e}\n\n{_current_text(row.content)}") from e

        try:
            return await self._update(name, apply, actor, described(reason, f"edit memory {name}"))
        except MemoryFull as e:
            raise MemoryFull(f"{e}\n\n{_current_text(e.current)}", e.current) from e

    async def restore(self, name: str, rev: int, actor: str) -> MemoryInfo:
        """把第 rev 版的正文作为新的一版写入，读版本与写入在同一个事务里。"""

        async def revision(_row: Item, s: AsyncSession) -> str:
            return await revision_doc_in(s, Subject.MEMORY, name, rev)

        return await self._update(name, revision, actor, f"restore memory {name}")

    async def delete(self, name: str, actor: str, *, reason: str | None = None) -> None:
        """全局记忆不能删（base 固定收录它）。"""
        if name == GLOBAL_MEMORY:
            raise InvalidInput(f"the global memory {GLOBAL_MEMORY!r} cannot be deleted")
        async with self._db.transaction() as s:
            row = await changeable_in(s, _KIND, name, actor)
            await s.delete(row)
            record_in(
                s, Subject.MEMORY, name, row.content, None, actor, described(reason, f"delete memory {name}")
            )

    async def _update(
        self,
        name: str,
        edit: Callable[[Item, AsyncSession], Awaitable[str]],
        actor: str,
        message: str,
    ) -> MemoryInfo:
        """读取、修改、写入与记版本在同一个写事务里：并发的修改互不覆盖。message 是这一版的说明。"""
        async with self._db.transaction() as s:
            row = await changeable_in(s, _KIND, name, actor)
            current = row.content
            updated = await edit(row, s)
            if len(updated) > self.max_chars:
                raise MemoryFull(self._full_message(name, len(updated), len(current)), current)
            row.content = updated
            record_in(s, Subject.MEMORY, name, current, updated, actor, message)
            await s.flush()
            return _info(row)

    def _check_size(self, size: int, name: str) -> None:
        if size > self.max_chars:
            raise InvalidInput(f"memory {name!r} would be {size} chars, limit is {self.max_chars}")

    def _full_message(self, name: str, size: int, now: int) -> str:
        # 给出要腾出的确切字数：只说「超了」时模型往往一次只缩短新条目几个字，反复撞墙
        return (
            f"memory {name!r} would be {size} chars, limit is {self.max_chars} (now {now}): free at least "
            f"{size - self.max_chars} chars first by condensing or deleting existing entries with memory_edit "
            "(aim for a few hundred chars of headroom), then retry"
        )


def _current_text(current: str) -> str:
    """报错里附的当前全文：cat -n 编号，分行同 text_edit.miss（splitlines），报错里说的行号才对得上。"""
    if not current:
        return "The memory is currently empty."
    return (
        "The memory may have changed since your run started (the copy in your instructions is not updated). "
        "Its current text follows, each line prefixed with its line number and a tab (not part of the text); "
        f"edit against this text:\n{text_edit.numbered(current.splitlines())}"
    )
