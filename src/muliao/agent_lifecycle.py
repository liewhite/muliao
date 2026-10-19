"""跨领域的 agent 生命周期操作。删除 agent 涉及会话领域（等待它的 agent 要收到错误、私有 thread 要一并删除），
在这里编排，agent 领域本身不依赖会话领域。"""

import logging

from sqlalchemy import select

from muliao.agents import AgentService
from muliao.db import Database
from muliao.errors import Conflict
from muliao.models import Session
from muliao.runtime.conversation import delete_sessions
from muliao.runtime.tasks import running_task_ids

log = logging.getLogger(__name__)


class AgentLifecycle:
    def __init__(self, db: Database, agents: AgentService):
        self._db = db
        self._agents = agents

    async def delete(self, agent_id: str) -> None:
        async with self._db.transaction() as s:
            agent = await self._agents.get_in(s, agent_id)
            if running := await running_task_ids(s, agent_id=agent_id):
                log.info("agent deletion refused: tasks running", extra={"agent": agent_id, "tasks": running})
                raise Conflict(
                    f"agent {agent.name!r} is working right now; wait for it to finish, then delete"
                )
            # 先按会话删除规则处理它的会话，再由外键级联删除其余数据（指令版本、私有 MCP 与 secret、触发器……）
            sessions = list((await s.scalars(select(Session).where(Session.agent_id == agent_id))).all())
            await delete_sessions(s, sessions)
            await s.delete(agent)
        self._agents.remove_files(agent_id)
