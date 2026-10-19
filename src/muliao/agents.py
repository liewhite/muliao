"""Agent 定义的增删改查及其文件目录。"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.exceptions import UserError
from pydantic_ai.models import infer_model
from sqlalchemy import select

from muliao.db import Database
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.evolution.memory import MemoryStore
from muliao.models import Agent
from muliao.naming import validate_slug


def check_model(model: str) -> None:
    """模型字符串必须能被 pydantic-ai 解析，且对应提供方的凭据已配置。"""
    try:
        infer_model(model)
    except UserError as e:
        raise InvalidInput(f"model {model!r} is not usable: {e}") from e


@dataclass(frozen=True)
class AgentSpec:
    description: str
    model: str
    instructions: str
    reflect_every: int


class AgentService:
    def __init__(
        self,
        db: Database,
        agents_dir: Path,
        memory: MemoryStore,
        model_checker: Callable[[str], None] = check_model,
    ):
        self._db = db
        self._agents_dir = agents_dir
        self._memory = memory
        self._check_model = model_checker

    def root(self, name: str) -> Path:
        return self._agents_dir / name

    def workspace(self, name: str) -> Path:
        return self.root(name) / "workspace"

    def _validate(self, spec: AgentSpec) -> None:
        if not spec.description.strip():
            raise InvalidInput("agent description must not be empty")
        if spec.reflect_every < 1:
            raise InvalidInput("reflect_every must be >= 1")
        self._check_model(spec.model)

    async def create(self, name: str, spec: AgentSpec) -> Agent:
        validate_slug("agent", name)
        self._validate(spec)
        async with self._db.transaction() as s:
            if await s.get(Agent, name) is not None:
                raise Conflict(f"agent {name!r} already exists")
            agent = Agent(
                name=name,
                description=spec.description,
                model=spec.model,
                instructions=spec.instructions,
                reflect_every=spec.reflect_every,
            )
            s.add(agent)
            # 目录与数据库行同事务创建：目录创建失败则回滚
            self.workspace(name).mkdir(parents=True, exist_ok=True)
            self._memory.init(name)
        return agent

    async def update(self, name: str, spec: AgentSpec) -> Agent:
        self._validate(spec)
        async with self._db.transaction() as s:
            agent = await s.get(Agent, name)
            if agent is None:
                raise NotFound(f"agent {name!r} not found")
            agent.description = spec.description
            agent.model = spec.model
            agent.instructions = spec.instructions
            agent.reflect_every = spec.reflect_every
        return agent

    async def get(self, name: str) -> Agent:
        async with self._db.transaction() as s:
            agent = await s.get(Agent, name)
            if agent is None:
                raise NotFound(f"agent {name!r} not found")
            return agent

    async def list(self) -> list[Agent]:
        async with self._db.transaction() as s:
            return list((await s.scalars(select(Agent).order_by(Agent.name))).all())

    async def delete(self, name: str) -> None:
        async with self._db.transaction() as s:
            agent = await s.get(Agent, name)
            if agent is None:
                raise NotFound(f"agent {name!r} not found")
            await s.delete(agent)
        # 删除目录，避免同名重建的 agent 继承旧记忆和 skill
        root = self.root(name)
        if root.exists():
            shutil.rmtree(root)
