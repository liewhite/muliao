"""Agent 定义的增删改查及其文件目录。"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.evolution.memory import MemoryStore
from muliao.evolution.prompts import PromptStore
from muliao.ids import new_agent_id
from muliao.models import Agent
from muliao.naming import normalize_agent_name
from muliao.resources import ResourceRepo


@dataclass(frozen=True)
class AgentDefaults:
    """新建 agent 时未指定的字段取这些值（来自服务端配置）；API 与工具共用这一份。"""

    model: str
    reflect_every: int
    # 所有可选的工具组名称（运行时注册的 ToolGroups）；新建 agent 默认全部开放
    tool_groups: tuple[str, ...]


@dataclass(frozen=True)
class AgentSpec:
    """新建 agent。model / reflect_every / tool_groups 为 None 时取 AgentDefaults。"""

    name: str
    description: str
    instructions: str
    model: str | None = None
    reflect_every: int | None = None
    tool_groups: list[str] | None = None


@dataclass(frozen=True)
class AgentPatch:
    """修改 agent：为 None 的字段保持不变。整体替换就是所有字段都给出。"""

    name: str | None = None
    description: str | None = None
    model: str | None = None
    instructions: str | None = None
    reflect_every: int | None = None
    tool_groups: list[str] | None = None


class AgentService:
    """agent 以系统生成的 id 标识（主键、目录名、外键）；名称只用于展示，可改、唯一。"""

    def __init__(
        self,
        db: Database,
        workspaces_dir: Path,
        repo: ResourceRepo,
        memory: MemoryStore,
        model_checker: Callable[[str], None],
        prompts: PromptStore,
        defaults: AgentDefaults,
    ):
        self._prompts = prompts
        self._db = db
        self._workspaces_dir = workspaces_dir
        self._repo = repo
        self._memory = memory
        self._check_model = model_checker
        self.defaults = defaults

    def workspace(self, agent_id: str) -> Path:
        return self._workspaces_dir / agent_id

    def _check(self, *, description: str, model: str, reflect_every: int, tool_groups: list[str]) -> None:
        if not description.strip():
            raise InvalidInput("agent description must not be empty")
        if reflect_every < 1:
            raise InvalidInput("reflect_every must be >= 1")
        unknown = [g for g in tool_groups if g not in self.defaults.tool_groups]
        if unknown or len(set(tool_groups)) != len(tool_groups):
            raise InvalidInput(
                f"tool groups must be distinct names from {list(self.defaults.tool_groups)}, got {tool_groups}"
            )
        self._check_model(model)

    async def _ensure_name_free(self, s: AsyncSession, name: str, agent_id: str | None) -> None:
        holder = await s.scalar(select(Agent.id).where(Agent.name == name))
        if holder is not None and holder != agent_id:
            # 只给名称：这条错误也会交还给模型，模型不接触 id
            raise Conflict(f"agent name {name!r} is already taken by another agent")

    async def create(self, spec: AgentSpec, *, author: str) -> Agent:
        """author：创建者，"user" 或创建它的 agent 的 id（记在指令的第一版上）。"""
        name = normalize_agent_name(spec.name)
        model = self.defaults.model if spec.model is None else spec.model
        reflect_every = self.defaults.reflect_every if spec.reflect_every is None else spec.reflect_every
        tool_groups = list(self.defaults.tool_groups) if spec.tool_groups is None else spec.tool_groups
        self._check(
            description=spec.description, model=model, reflect_every=reflect_every, tool_groups=tool_groups
        )
        async with self._db.transaction() as s:
            await self._ensure_name_free(s, name, None)
            agent = Agent(
                id=new_agent_id(),
                name=name,
                description=spec.description,
                model=model,
                reflect_every=reflect_every,
                tool_groups=tool_groups,
            )
            s.add(agent)
            # 目录与数据库行同事务创建：目录创建失败则回滚
            self.workspace(agent.id).mkdir(parents=True, exist_ok=True)
        await self._repo.apply(
            {self._memory.path(agent.id): "", self._prompts.path(agent.id): spec.instructions},
            f"create agent {agent.id}",
            author,
        )
        return agent

    async def update(self, agent_id: str, patch: AgentPatch, *, author: str) -> Agent:
        """在一个事务里读取并只修改给出的字段：并发的局部修改互不覆盖。指令有变化时追加一版（author 同 create）。"""
        async with self._db.transaction() as s:
            agent = await self._get(s, agent_id)
            name = agent.name if patch.name is None else normalize_agent_name(patch.name)
            description = agent.description if patch.description is None else patch.description
            model = agent.model if patch.model is None else patch.model
            reflect_every = agent.reflect_every if patch.reflect_every is None else patch.reflect_every
            tool_groups = agent.tool_groups if patch.tool_groups is None else patch.tool_groups
            self._check(
                description=description, model=model, reflect_every=reflect_every, tool_groups=tool_groups
            )
            await self._ensure_name_free(s, name, agent_id)
            agent.name, agent.description, agent.model = name, description, model
            agent.reflect_every = reflect_every
            # JSON 列只在整体赋值时被追踪
            agent.tool_groups = list(tool_groups)
        # 指令在资源仓库里，单独提交；数据库这边已提交，提交失败时错误可见、文件留待下一次提交
        if patch.instructions is not None and await self._prompts.set(agent_id, patch.instructions, author):
            async with self._db.transaction() as s:
                agent = await self._get(s, agent_id)
                agent.updated_at = utcnow()
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

    async def get_in(self, s: AsyncSession, agent_id: str) -> Agent:
        return await self._get(s, agent_id)

    async def remove_files(self, agent_id: str, author: str) -> None:
        """删除 agent 的工作目录与私有资源（记忆、指令、skill、MCP 配置，删除记入资源仓库）。数据库行由调用方删除。"""
        workspace = self.workspace(agent_id)
        if workspace.exists():
            shutil.rmtree(workspace)
        await self._repo.apply({self._repo.owner_dir(agent_id): None}, f"delete agent {agent_id}", author)
