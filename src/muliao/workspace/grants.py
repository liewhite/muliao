"""skill 脚本的「始终允许」（#531）：用户批准一次等批准的 skill_run 调用时选「始终允许」，之后同一个调用直接执行，
不再停下等批准。「同一个调用」：同一个 agent（按 id，改名不丢）、同一 skill 的同一脚本、同样的参数与注入的 secret，
且 skill 目录里除 SKILL.md 外的内容没变（改了脚本或它用的辅助文件就要重新批准，改说明不用）。timeout、background、
heartbeat_seconds 不算在内。没有通配与前缀匹配。

授权存在 script_grants 表（不放资源目录：agent 能写自己的资源目录）。用户在设置页查看、撤销；点「始终允许」不会顺带
放行别的会话里已经在等的同一调用（那些各自等用户的决定）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.events import AgentDeleting
from muliao.errors import NotFound
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit
from muliao.workspace.tables import ScriptGrant

# 按始终允许直接执行时，接在结果前面告诉模型（也留在会话记录里）
GRANTED_NOTE = "(Run without asking: the user always allows this exact call.)"


@dataclass(frozen=True)
class ScriptCall:
    """一次 skill_run 调用里决定「是不是同一个调用」的部分。"""

    agent_id: str
    skill: str
    # 相对 skill 目录的脚本路径（解析后的）
    script: str
    args: tuple[str, ...]
    secrets: tuple[str, ...]
    # agents.SkillScripts.fingerprint
    fingerprint: str

    @classmethod
    def of(
        cls,
        agent_id: str,
        skill: str,
        script: str,
        args: tuple[str, ...],
        secrets: tuple[str, ...],
        fingerprint: str,
    ) -> ScriptCall:
        # secret 的顺序与重复不影响注入的结果
        return cls(agent_id, skill, script, tuple(args), tuple(sorted(set(secrets))), fingerprint)

    @property
    def key(self) -> str:
        data = [self.agent_id, self.skill, self.script, list(self.args), list(self.secrets), self.fingerprint]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class GrantInfo:
    id: int
    agent_id: str
    skill: str
    script: str
    args: tuple[str, ...]
    secrets: tuple[str, ...]
    session_id: str
    created_at: datetime
    hits: int
    last_used_at: datetime | None


class ScriptGrants:
    def __init__(self, db: Database):
        self._db = db

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "workspace.script_grants.agent_deleting", self.on_agent_deleting)

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：删除给它的始终允许。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(delete(ScriptGrant).where(ScriptGrant.agent_id == event.agent_id))

        return commit

    async def find(self, call: ScriptCall) -> int | None:
        """这个调用有始终允许时返回它的 id。"""
        async with self._db.transaction() as s:
            return await s.scalar(select(ScriptGrant.id).where(ScriptGrant.key == call.key))

    async def used(self, grant_id: int) -> None:
        """按这条始终允许直接执行了一次。期间被撤销的不算（行已删，什么也不改）。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(ScriptGrant)
                .where(ScriptGrant.id == grant_id)
                .values(hits=ScriptGrant.hits + 1, last_used_at=utcnow())
            )

    async def grant(self, call: ScriptCall, session_id: str, call_id: str) -> None:
        """用户对这次调用点了「始终允许」。已有同一调用的始终允许时不再添一条。"""
        async with self._db.transaction() as s:
            if await s.scalar(select(ScriptGrant.id).where(ScriptGrant.key == call.key)) is not None:
                return
            s.add(
                ScriptGrant(
                    agent_id=call.agent_id,
                    skill=call.skill,
                    script=call.script,
                    args=list(call.args),
                    secrets=list(call.secrets),
                    fingerprint=call.fingerprint,
                    key=call.key,
                    session_id=session_id,
                    call_id=call_id,
                )
            )

    async def list(self) -> list[GrantInfo]:
        """全部始终允许，新的在前。"""
        async with self._db.transaction() as s:
            rows = (await s.scalars(select(ScriptGrant).order_by(ScriptGrant.id.desc()))).all()
        return [
            GrantInfo(
                r.id,
                r.agent_id,
                r.skill,
                r.script,
                tuple(r.args),
                tuple(r.secrets),
                r.session_id,
                r.created_at,
                r.hits,
                r.last_used_at,
            )
            for r in rows
        ]

    async def revoke(self, grant_id: int) -> None:
        """撤销一条始终允许：之后同一调用重新要批准。"""
        async with self._db.transaction() as s:
            result = await s.execute(delete(ScriptGrant).where(ScriptGrant.id == grant_id))
            if result.rowcount == 0:
                raise NotFound(f"script grant {grant_id} not found")
