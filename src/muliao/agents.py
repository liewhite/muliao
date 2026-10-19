"""Agent 定义的增删改查及其文件目录。"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.evolution.memory import MemoryStore
from muliao.ids import new_agent_id
from muliao.models import Agent
from muliao.naming import normalize_agent_name
from muliao.runtime.tasks import running_task_ids


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int


class AgentService:
    """agent 以系统生成的 id 标识（主键、目录名、外键）；名称只用于展示，可改、唯一。"""

    def __init__(
        self,
        db: Database,
        agents_dir: Path,
        memory: MemoryStore,
        model_checker: Callable[[str], None],
    ):
        self._db = db
        self._agents_dir = agents_dir
        self._memory = memory
        self._check_model = model_checker

    def root(self, agent_id: str) -> Path:
        return self._agents_dir / agent_id

    def workspace(self, agent_id: str) -> Path:
        return self.root(agent_id) / "workspace"

    def _validated(self, spec: AgentSpec) -> AgentSpec:
        if not spec.description.strip():
            raise InvalidInput("agent description must not be empty")
        if spec.reflect_every < 1:
            raise InvalidInput("reflect_every must be >= 1")
        self._check_model(spec.model)
        return replace(spec, name=normalize_agent_name(spec.name))

    async def _ensure_name_free(self, s: AsyncSession, name: str, agent_id: str | None) -> None:
        holder = await s.scalar(select(Agent.id).where(Agent.name == name))
        if holder is not None and holder != agent_id:
            raise Conflict(f"agent name {name!r} is already used by agent {holder}")

    async def create(self, spec: AgentSpec) -> Agent:
        spec = self._validated(spec)
        async with self._db.transaction() as s:
            await self._ensure_name_free(s, spec.name, None)
            agent = Agent(
                id=new_agent_id(),
                name=spec.name,
                description=spec.description,
                model=spec.model,
                instructions=spec.instructions,
                reflect_every=spec.reflect_every,
            )
            s.add(agent)
            # 目录与数据库行同事务创建：目录创建失败则回滚
            self.workspace(agent.id).mkdir(parents=True, exist_ok=True)
            self._memory.init(agent.id)
        return agent

    async def update(self, agent_id: str, spec: AgentSpec) -> Agent:
        spec = self._validated(spec)
        async with self._db.transaction() as s:
            agent = await self._get(s, agent_id)
            await self._ensure_name_free(s, spec.name, agent_id)
            agent.name = spec.name
            agent.description = spec.description
            agent.model = spec.model
            agent.instructions = spec.instructions
            agent.reflect_every = spec.reflect_every
        return agent

    async def _get(self, s: AsyncSession, agent_id: str) -> Agent:
        agent = await s.get(Agent, agent_id)
        if agent is None:
            raise NotFound(f"agent {agent_id!r} not found")
        return agent

    async def get(self, agent_id: str) -> Agent:
        async with self._db.transaction() as s:
            return await self._get(s, agent_id)

    async def find_by_name(self, name: str) -> Agent:
        """模型按名称指代 agent（不接触 id）：在工具层把名称解析为 agent。名称唯一。"""
        name = normalize_agent_name(name)
        async with self._db.transaction() as s:
            agent = await s.scalar(select(Agent).where(Agent.name == name))
            if agent is None:
                names = (await s.scalars(select(Agent.name).order_by(Agent.name))).all()
                raise NotFound(f"no agent named {name!r}; available agents: {list(names)}")
            return agent

    async def list(self) -> list[Agent]:
        async with self._db.transaction() as s:
            return list((await s.scalars(select(Agent).order_by(Agent.name))).all())

    async def delete(self, agent_id: str) -> None:
        async with self._db.transaction() as s:
            agent = await self._get(s, agent_id)
            if running := await running_task_ids(s, agent_id=agent_id):
                raise Conflict(f"agent {agent.name!r} has running tasks {running}; wait for them to finish")
            await s.delete(agent)
        root = self.root(agent_id)
        if root.exists():
            shutil.rmtree(root)
