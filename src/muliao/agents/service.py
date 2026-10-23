"""Agent 定义的增删改查及其文件目录。"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast, get_args

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import active_row_in
from muliao.agents.ids import new_agent_id
from muliao.agents.memory import MemoryStore
from muliao.agents.naming import new_agent_name, normalize_agent_name
from muliao.agents.resources import ResourceRepo
from muliao.agents.tables import Agent
from muliao.agents.tool_catalog import ToolGroupInfo
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database

log = logging.getLogger(__name__)

# agent 的思考档位，运行时交给 pydantic_ai 的 ModelSettings.thinking（agent_runtime.llm.model_settings）：
# "default" 什么都不传，按模型与服务端的默认；"off" 关闭；其余是 effort 档位。各 provider 怎么换算、模型不支持
# 时怎么处理都由 pydantic_ai 与服务端决定，这里不映射
Thinking = Literal["default", "off", "low", "medium", "high", "xhigh"]
THINKING_LEVELS: tuple[Thinking, ...] = get_args(Thinking)


@dataclass(frozen=True)
class AgentDefaults:
    """新建 agent 时未指定的字段取这些值（来自服务端配置）；API 与工具共用这一份。"""

    model: str
    # 当前配置下可选的工具组（tool_catalog.available）：校验与默认值都从它取
    groups: tuple[ToolGroupInfo, ...]

    @property
    def group_names(self) -> list[str]:
        return [g.name for g in self.groups]


@dataclass(frozen=True)
class AgentSpec:
    """新建 agent。model / tool_groups 为 None 时取 AgentDefaults。"""

    name: str
    description: str
    model: str | None = None
    tool_groups: list[str] | None = None
    thinking: Thinking = "default"


@dataclass(frozen=True)
class AgentPatch:
    """修改 agent：为 None 的字段保持不变。整体替换就是所有字段都给出。"""

    name: str | None = None
    description: str | None = None
    model: str | None = None
    tool_groups: list[str] | None = None
    thinking: Thinking | None = None


@dataclass(frozen=True)
class _AgentFields:
    """agent 的可修改字段合并 patch 之后的值（AgentService._validated）。"""

    name: str
    description: str
    model: str
    tool_groups: list[str]
    thinking: Thinking


@dataclass(frozen=True)
class AgentInfo:
    """别的领域看到的 agent（读模型）：ORM 行不出 agent 领域。"""

    id: str
    name: str
    description: str
    model: str
    tool_groups: tuple[str, ...]
    thinking: Thinking
    created_at: datetime
    # 系统默认 agent（Web 新对话默认选中它），最多一个
    is_default: bool


def info_of(agent: Agent) -> AgentInfo:
    return AgentInfo(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        model=agent.model,
        tool_groups=tuple(agent.tool_groups),
        # 写入时与启动时已校验（_check_thinking_level）
        thinking=cast(Thinking, agent.thinking),
        created_at=agent.created_at,
        is_default=agent.is_default,
    )


def _check_thinking_level(thinking: str) -> None:
    """写入时与启动检查时同一条规则（库里的值经 info_of 直接当作 Thinking 用）。"""
    if thinking not in THINKING_LEVELS:
        raise InvalidInput(f"thinking must be one of {list(THINKING_LEVELS)}, got {thinking!r}")


async def active_agent_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """还可用的 agent（正在删除的按不存在处理）。别的领域在自己的写事务里为某个 agent 写数据之前用它检查：事务串行，
    agent 只会从可用变为正在删除、再变为不存在，检查与写入之间不会插进删除，删除流程的清理一定看得到这次写入。"""
    return info_of(await active_row_in(s, agent_id))


@dataclass(frozen=True)
class AgentNames:
    """agent id → 展示名称（包括正在删除的）。别的领域里留着的 id 可能属于已删除的 agent，显示为
    「(deleted agent <id>)」，而不是报错或露出裸 id。"""

    by_id: dict[str, str]

    def __getitem__(self, agent_id: str) -> str:
        return self.by_id.get(agent_id, f"(deleted agent {agent_id})")


async def agent_names_in(s: AsyncSession) -> AgentNames:
    """在调用方的事务里取全部 agent 的名称（AgentNames）。"""
    return AgentNames({agent_id: name for agent_id, name in await s.execute(select(Agent.id, Agent.name))})


async def agent_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """agent（包括正在删除的）。在调用方的事务里读它的展示信息（名称、模型）：这些字段的修改与调用方的写入之间没有
    先后要求。"""
    return info_of(await _row_in(s, agent_id))


async def _row_in(s: AsyncSession, agent_id: str) -> Agent:
    """agent 的行（包括正在删除的）；不存在报 NotFound。"""
    agent = await s.get(Agent, agent_id)
    if agent is None:
        raise NotFound(f"agent {agent_id!r} not found")
    return agent


class AgentService:
    """agent 以系统生成的 id 标识（主键、目录名、外键）；名称只用于展示，可改、唯一。"""

    def __init__(
        self,
        db: Database,
        workspaces_dir: Path,
        repo: ResourceRepo,
        memory: MemoryStore,
        # (模型, 思考档位) 在当前配置下不能运行时报 InvalidInput（agent_runtime.llm.ModelRegistry.check）
        model_checker: Callable[[str, Thinking], None],
        defaults: AgentDefaults,
    ):
        self._db = db
        self._workspaces_dir = workspaces_dir
        self._repo = repo
        self._memory = memory
        self._check_model = model_checker
        self.defaults = defaults

    def workspace(self, agent_id: str) -> Path:
        return self._workspaces_dir / agent_id

    def _workspace_owners(self) -> list[str]:
        """有工作目录的 agent id。"""
        root = self._workspaces_dir
        return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []

    def _unavailable_groups(self, tool_groups: Sequence[str]) -> list[str]:
        """不在当前配置可用的组里的组名（例如去掉 [web] 配置后的 web）。"""
        return [g for g in tool_groups if g not in self.defaults.group_names]

    async def verify_stored(self) -> None:
        """启动检查：已存 agent 的模型与思考档位必须能按当前配置运行、选用的工具组必须都可用，与新建、修改时同一套规则。
        配置改动（删 provider、改名、去掉 [web]）在启动时暴露，而不是散落到每个任务里失败。正在删除的不查。
        先报出已删除 agent 的残留目录（只记录，不阻止启动）。"""
        await self._report_leftovers()
        agents = await self.list()
        broken = []
        for agent in agents:
            try:
                _check_thinking_level(agent.thinking)
                self._check_model(agent.model, agent.thinking)
            except InvalidInput as e:
                broken.append(f"{agent.name} ({agent.id}): {e}")
        if broken:
            raise RuntimeError(
                "agents reference models or thinking levels not runnable with current config:\n"
                + "\n".join(broken)
            )
        broken = [
            f"{a.name} ({a.id}): {sorted(unknown)}"
            for a in agents
            if (unknown := self._unavailable_groups(a.tool_groups))
        ]
        if broken:
            raise RuntimeError(
                "agents use tool groups unavailable with current config:\n" + "\n".join(broken)
            )

    async def _report_leftovers(self) -> None:
        """数据库里已删除、还留着资源目录（删除时提交失败所致）或工作目录（删除时没删干净）的 agent：记录出来，由人决定
        是否清理。正在删除的不算：它的目录由删除流程删。"""
        known = await self._ids()
        orphans = [o for o in self._repo.agent_owners() if o not in known]
        if orphans:
            log.warning("resource directories of deleted agents remain", extra={"agent_ids": orphans})
        leftovers = [o for o in self._workspace_owners() if o not in known]
        if leftovers:
            log.warning("workspaces of deleted agents remain", extra={"agent_ids": leftovers})

    def _check(self, *, description: str, model: str, tool_groups: list[str], thinking: Thinking) -> None:
        _check_thinking_level(thinking)
        if not description.strip():
            raise InvalidInput("agent description must not be empty")
        unknown = self._unavailable_groups(tool_groups)
        if unknown or len(set(tool_groups)) != len(tool_groups):
            raise InvalidInput(
                f"tool groups must be distinct names from {self.defaults.group_names}, got {tool_groups}"
            )
        self._check_model(model, thinking)

    async def _ensure_name_free(self, s: AsyncSession, name: str, agent_id: str | None) -> None:
        holder = await s.scalar(select(Agent.id).where(Agent.name == name))
        if holder is not None and holder != agent_id:
            # 只给名称：这条错误也会交还给模型，模型不接触 id
            raise Conflict(f"agent name {name!r} is already taken by another agent")

    async def create(self, spec: AgentSpec, *, author: str) -> AgentInfo:
        """author：创建者，"user" 或创建它的 agent 的 id（记在记忆的第一版上）。新 agent 的记忆是空的。"""
        name = new_agent_name(spec.name)
        model = self.defaults.model if spec.model is None else spec.model
        tool_groups = self.defaults.group_names if spec.tool_groups is None else spec.tool_groups
        self._check(
            description=spec.description, model=model, tool_groups=tool_groups, thinking=spec.thinking
        )
        async with self._db.transaction() as s:
            await self._ensure_name_free(s, name, None)
        # 先写资源文件、后插数据库行：agent 一旦存在，它的记忆就必须在（每一轮都要读）。
        # 资源写入失败时什么都没留下（apply 原子），报错就是没有生效；只有并发创建同名 agent 时下面的检查会冲突，
        # 留下的资源目录由启动时的孤儿检查报告（与删除 agent 时提交失败同一种情况）
        agent_id = new_agent_id()
        await self._repo.apply(
            {self._memory.path(agent_id): ""},
            f"create agent {agent_id}",
            author,
            # 它的行在资源写入之后才插入：此刻还不是可用的 owner
            unchecked=True,
        )
        async with self._db.transaction() as s:
            await self._ensure_name_free(s, name, None)
            agent = Agent(
                id=agent_id,
                name=name,
                description=spec.description,
                model=model,
                tool_groups=tool_groups,
                thinking=spec.thinking,
            )
            s.add(agent)
            # 工作目录与数据库行同事务：建不出来则不插行
            self.workspace(agent_id).mkdir(parents=True, exist_ok=True)
            await s.flush()
            return info_of(agent)

    async def update(self, agent_id: str, patch: AgentPatch) -> AgentInfo:
        """在一个事务里读取并只修改给出的字段：并发的局部修改互不覆盖。"""
        async with self._db.transaction() as s:
            agent = await active_row_in(s, agent_id)
            fields = self._validated(agent, patch)
            await self._ensure_name_free(s, fields.name, agent_id)
            agent.name, agent.description, agent.model = fields.name, fields.description, fields.model
            agent.thinking = fields.thinking
            # JSON 列只在整体赋值时被追踪
            agent.tool_groups = list(fields.tool_groups)
            await s.flush()
            return info_of(agent)

    async def set_default(self, agent_id: str, is_default: bool) -> AgentInfo:
        """设为（或取消）系统默认 agent。设为默认时同一事务里先清掉旧的，始终最多一个；取消不是默认的 agent 不做任何事。"""
        async with self._db.transaction() as s:
            agent = await active_row_in(s, agent_id)
            if is_default:
                await s.execute(
                    update(Agent).where(Agent.is_default, Agent.id != agent_id).values(is_default=False)
                )
            agent.is_default = is_default
            await s.flush()
            return info_of(agent)

    def _validated(self, agent: Agent, patch: AgentPatch) -> _AgentFields:
        """patch 合并到 agent 当前值之后的字段，校验过（不合法时报 InvalidInput）。"""
        fields = _AgentFields(
            name=agent.name if patch.name is None else new_agent_name(patch.name),
            description=agent.description if patch.description is None else patch.description,
            model=agent.model if patch.model is None else patch.model,
            tool_groups=agent.tool_groups if patch.tool_groups is None else patch.tool_groups,
            thinking=cast(Thinking, agent.thinking) if patch.thinking is None else patch.thinking,
        )
        self._check(
            description=fields.description,
            model=fields.model,
            tool_groups=fields.tool_groups,
            thinking=fields.thinking,
        )
        return fields

    async def get_active(self, agent_id: str) -> AgentInfo:
        """还可用的 agent：正在删除的按不存在处理（例如读它的私有资源时）。为它写数据的检查在写入的地方：数据库数据在
        同一事务里用 active_agent_in，私有资源由资源仓库在它的锁里检查（agents.active，ResourceRepo）。"""
        async with self._db.transaction() as s:
            return await active_agent_in(s, agent_id)

    async def get(self, agent_id: str) -> AgentInfo:
        """agent（包括正在删除的）。"""
        async with self._db.transaction() as s:
            return await agent_in(s, agent_id)

    async def find_by_name(self, name: str) -> AgentInfo:
        """模型按名称指代 agent（不接触 id）：在工具层把名称解析为 agent。名称唯一。"""
        name = normalize_agent_name(name)
        async with self._db.transaction() as s:
            agent = await s.scalar(select(Agent).where(Agent.name == name, Agent.deleting_by.is_(None)))
            if agent is None:
                names = (
                    await s.scalars(
                        select(Agent.name).where(Agent.deleting_by.is_(None)).order_by(Agent.name)
                    )
                ).all()
                raise NotFound(f"no agent named {name!r}; available agents: {list(names)}")
            return info_of(agent)

    async def names(self) -> AgentNames:
        async with self._db.transaction() as s:
            return await agent_names_in(s)

    async def list(self) -> list[AgentInfo]:
        async with self._db.transaction() as s:
            # 正在删除的不列出
            rows = (
                await s.scalars(select(Agent).where(Agent.deleting_by.is_(None)).order_by(Agent.name))
            ).all()
            return [info_of(a) for a in rows]

    async def _ids(self) -> set[str]:
        """数据库里所有 agent 的 id，正在删除的也算（它的目录还在是正常的，删除流程会删）。"""
        async with self._db.transaction() as s:
            return set((await s.scalars(select(Agent.id))).all())

    async def remove_files(self, agent_id: str, author: str) -> None:
        """删除 agent 的工作目录与私有资源（记忆、skill、MCP 配置，删除记入资源仓库）。数据库行由调用方删除。"""
        workspace = self.workspace(agent_id)
        if workspace.exists():
            # 工作目录里可能有 venv、clone 的仓库、后台任务日志：放到线程里删，不阻塞事件循环
            try:
                await asyncio.to_thread(shutil.rmtree, workspace)
            except OSError as e:
                # 例如它留在后台的进程还在往里写（目录删空后又出现新文件）。工作目录只是文件：删不干净不能让删除卡在
                # deleting 一直重试；残留留给启动检查列出（verify_stored），由人清理
                log.warning(
                    "workspace of a deleted agent could not be fully removed",
                    extra={"agent": agent_id, "path": str(workspace), "error": repr(e)},
                )
        # 它已是正在删除（不再可用）：在同一把锁里删掉目录，之前检查通过的写入都在它之前（agents.active）
        await self._repo.apply(
            {self._repo.owner_dir(agent_id): None}, f"delete agent {agent_id}", author, unchecked=True
        )
