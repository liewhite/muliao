"""agent 目录（子域）：agent 定义的增删改查及其工作目录，agents 表只在这里与 agents.active 读写。新建与修改带着它的分发规则时，
在同一个写事务里经分发子域的接口写入（agents.agent_rules，记一版），新建时经记忆子域建空记忆；其余字段（名称、描述、模型……）
不记版本。删除与归档的流程在 agents.lifecycle，它对 agents 表的写入用这里的 `*_in`。"""

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

from muliao.agents.active import AgentNames, active_row_in, agent_names_in, runnable_row_in
from muliao.agents.agent_rules import (
    AgentRules,
    agent_rules_in,
    all_agent_rules_in,
    create_agent_rules_in,
    update_agent_rules_in,
)
from muliao.agents.ids import new_agent_id
from muliao.agents.memory import create_memory_in
from muliao.agents.naming import new_agent_name, normalize_agent_name
from muliao.agents.tables import Agent
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database, utcnow

log = logging.getLogger(__name__)

# agent 的思考档位，运行时交给 pydantic_ai 的 ModelSettings.thinking（agent_runtime.llm.model_settings）：
# "default" 什么都不传，按模型与服务端的默认；"off" 关闭；其余是 effort 档位。各 provider 怎么换算、模型不支持
# 时怎么处理都由 pydantic_ai 与服务端决定，这里不映射
Thinking = Literal["default", "off", "low", "medium", "high", "xhigh"]
THINKING_LEVELS: tuple[Thinking, ...] = get_args(Thinking)

GROUP_MAX = 64


def normalize_group(group: str) -> str | None:
    """分组名去掉首尾空白；空串即不分组（None）。"""
    group = group.strip()
    if len(group) > GROUP_MAX:
        raise InvalidInput(f"agent group must be at most {GROUP_MAX} characters, got {len(group)}")
    return group or None


@dataclass(frozen=True)
class AgentDefaults:
    """新建 agent 时未指定的字段取这些值（来自服务端配置）；API 与工具共用这一份。"""

    model: str


@dataclass(frozen=True)
class AgentSpec:
    """新建 agent。model 为 None 时取 AgentDefaults。roles 不含 base（每个 agent 隐含）。"""

    name: str
    description: str
    model: str | None = None
    roles: Sequence[str] = ()
    allow: Sequence[str] = ()
    deny: Sequence[str] = ()
    thinking: Thinking = "default"
    # 分组名（normalize_group：空串与 None 都是不分组）
    group: str | None = None


@dataclass(frozen=True)
class AgentPatch:
    """修改 agent：为 None 的字段保持不变。整体替换就是所有字段都给出（group 的空串是「不分组」）。归档不在这里：
    它要查 agent 是否空闲，见 AgentLifecycle.set_archived。"""

    name: str | None = None
    description: str | None = None
    model: str | None = None
    roles: Sequence[str] | None = None
    allow: Sequence[str] | None = None
    deny: Sequence[str] | None = None
    thinking: Thinking | None = None
    group: str | None = None


@dataclass(frozen=True)
class _AgentFields:
    """agent 的可修改字段合并 patch 之后的值（AgentService._validated）。"""

    name: str
    description: str
    model: str
    thinking: Thinking
    group: str | None


@dataclass(frozen=True)
class AgentInfo:
    """别的领域看到的 agent（读模型）：ORM 行不出 agent 领域。"""

    id: str
    name: str
    description: str
    model: str
    thinking: Thinking
    created_at: datetime
    # 系统默认 agent（Web 新对话默认选中它），最多一个
    is_default: bool
    # 分组名，None 为不分组
    group: str | None
    # 归档时间；非空时不能运行（agents.active.runnable_row_in）
    archived_at: datetime | None

    @property
    def archived(self) -> bool:
        return self.archived_at is not None


def info_of(agent: Agent) -> AgentInfo:
    return AgentInfo(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        model=agent.model,
        # 写入时与启动时已校验（_check_thinking_level）
        thinking=cast(Thinking, agent.thinking),
        created_at=agent.created_at,
        is_default=agent.is_default,
        group=agent.group,
        archived_at=agent.archived_at,
    )


def _check_thinking_level(thinking: str) -> None:
    """写入时与启动检查时同一条规则（库里的值经 info_of 直接当作 Thinking 用）。"""
    if thinking not in THINKING_LEVELS:
        raise InvalidInput(f"thinking must be one of {list(THINKING_LEVELS)}, got {thinking!r}")


async def active_agent_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """还可用的 agent（正在删除的按不存在处理）。别的领域在自己的写事务里为某个 agent 写数据之前用它检查：事务串行，
    agent 只会从可用变为正在删除、再变为不存在，检查与写入之间不会插进删除，删除流程的清理一定看得到这次写入。"""
    return info_of(await active_row_in(s, agent_id))


async def runnable_agent_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """能运行的 agent：还可用且没有归档，归档的报 AgentArchived（Conflict）。给它派新工作的入口用它：来信与新会话
    （sessions.inbound）、指派与 owner（issues）、触发器的创建与触发。管理与资源的写入（改配置、记忆、secret）用 active_agent_in，
    归档的照常可改。"""
    return info_of(await runnable_row_in(s, agent_id))


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


async def _listed_in(s: AsyncSession) -> list[Agent]:
    """列表里的 agent（正在删除的不列出），按名称。"""
    return list(await s.scalars(select(Agent).where(Agent.deleting_by.is_(None)).order_by(Agent.name)))


# ---- 删除与归档（agents.lifecycle 编排）对 agents 表的写入 ----


async def deletable_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """可以开始删除的 agent：存在且不在删除中（否则 NotFound / Conflict）。"""
    agent = await _row_in(s, agent_id)
    if agent.deleting_by is not None:
        raise Conflict(f"agent {agent.name!r} is already being deleted")
    return info_of(agent)


async def mark_deleting_in(s: AsyncSession, agent_id: str, actor: str) -> None:
    """标为正在删除（改的人 actor）：之后不出现在列表与名称解析里，各领域的写入口按不存在处理。"""
    await deletable_in(s, agent_id)
    (await _row_in(s, agent_id)).deleting_by = actor


async def deletion_actor_in(s: AsyncSession, agent_id: str) -> str | None:
    """正在删除的 agent 的删除作者；行已删除时 None（删除流程重放）。没在删除中说明流程与数据不一致，报错。"""
    agent = await s.get(Agent, agent_id)
    if agent is None:
        return None
    if agent.deleting_by is None:
        raise RuntimeError(f"agent {agent_id} had its sessions cleared but is not being deleted")
    return agent.deleting_by


async def remove_in(s: AsyncSession, agent_id: str) -> None:
    """删除 agent 的行：它名下还归它的条目、记忆与分发规则随之级联删除（各子域先在同一事务里记好删除的版本）。"""
    await s.delete(await _row_in(s, agent_id))


def check_archivable(agent: AgentInfo) -> None:
    if agent.is_default:
        raise Conflict(f"agent {agent.name!r} is the default agent; unset it as default first, then archive")


async def set_archived_in(s: AsyncSession, agent_id: str, archived: bool) -> AgentInfo:
    """归档或取消归档还可用的 agent；已是目标状态时不改。归档要求它不是系统默认 agent（check_archivable）。"""
    agent = await active_row_in(s, agent_id)
    if (agent.archived_at is not None) != archived:
        if archived:
            check_archivable(info_of(agent))
        agent.archived_at = utcnow() if archived else None
        await s.flush()
    return info_of(agent)


class AgentService:
    """agent 以系统生成的 id 标识（主键、目录名、外键）；名称只用于展示，可改、唯一。"""

    def __init__(
        self,
        db: Database,
        workspaces_dir: Path,
        # (模型, 思考档位) 在当前配置下不能运行时报 InvalidInput（agent_runtime.llm.ModelRegistry.check）
        model_checker: Callable[[str, Thinking], None],
        defaults: AgentDefaults,
    ):
        self._db = db
        self._workspaces_dir = workspaces_dir
        self._check_model = model_checker
        self.defaults = defaults

    def workspace(self, agent_id: str) -> Path:
        return self._workspaces_dir / agent_id

    def _workspace_owners(self) -> list[str]:
        """有工作目录的 agent id。"""
        root = self._workspaces_dir
        return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []

    async def verify_stored(self) -> None:
        """启动检查：已存 agent 的模型与思考档位必须能按当前配置运行，与新建、修改时同一套规则。配置改动（删 provider）
        在启动时暴露，而不是散落到每个任务里失败。正在删除的不查。先报出已删除 agent 的残留工作目录（只记录，不阻止启动）。
        分发规则的检查在 Distribution.verify_stored。"""
        await self._report_leftovers()
        broken = []
        for agent in await self.list():
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

    async def _report_leftovers(self) -> None:
        """数据库里已删除、还留着工作目录（删除时没删干净）的 agent：记录出来，由人决定是否清理。正在删除的不算：
        它的目录由删除流程删。"""
        known = await self._ids()
        leftovers = [o for o in self._workspace_owners() if o not in known]
        if leftovers:
            log.warning("workspaces of deleted agents remain", extra={"agent_ids": leftovers})

    def _check(self, fields: _AgentFields) -> None:
        """不依赖数据库的校验；分发规则在写事务里由分发子域校验（agent_rules.check_agent_rules_in）。"""
        _check_thinking_level(fields.thinking)
        if not fields.description.strip():
            raise InvalidInput("agent description must not be empty")
        self._check_model(fields.model, fields.thinking)

    async def _ensure_name_free(self, s: AsyncSession, name: str, agent_id: str | None) -> None:
        holder = await s.scalar(select(Agent.id).where(Agent.name == name))
        if holder is not None and holder != agent_id:
            # 只给名称：这条错误也会交还给模型，模型不接触 id
            raise Conflict(f"agent name {name!r} is already taken by another agent")

    async def create(self, spec: AgentSpec, *, actor: str) -> AgentInfo:
        """actor：创建者，"user" 或创建它的 agent 的 id（记在记忆与规则的第一版上）。新 agent 的记忆是空的。"""
        fields = _AgentFields(
            name=new_agent_name(spec.name),
            description=spec.description,
            model=self.defaults.model if spec.model is None else spec.model,
            thinking=spec.thinking,
            group=None if spec.group is None else normalize_group(spec.group),
        )
        self._check(fields)
        name = fields.name
        agent_id = new_agent_id()
        async with self._db.transaction() as s:
            await self._ensure_name_free(s, name, None)
            agent = Agent(
                id=agent_id,
                name=name,
                description=fields.description,
                model=fields.model,
                thinking=fields.thinking,
                group=fields.group,
            )
            s.add(agent)
            await s.flush()
            # 规则与记忆和 agent 的行同一事务：agent 一旦存在，它的规则与记忆就在（每一轮都要读）
            await create_agent_rules_in(
                s, agent_id, AgentRules.of(spec.roles, spec.allow, spec.deny), actor, f"create agent {name}"
            )
            create_memory_in(s, agent_id, actor)
            # 工作目录与数据库行同事务：建不出来则不插行
            self.workspace(agent_id).mkdir(parents=True, exist_ok=True)
            return info_of(agent)

    async def update(self, agent_id: str, patch: AgentPatch, *, actor: str) -> AgentInfo:
        """在一个事务里读取并只修改给出的字段：并发的局部修改互不覆盖。分发规则变了就记一版（改的人 actor）。"""
        async with self._db.transaction() as s:
            agent = await active_row_in(s, agent_id)
            fields = self._validated(agent, patch)
            await self._ensure_name_free(s, fields.name, agent_id)
            before = await agent_rules_in(s, agent_id)
            rules = AgentRules.of(
                before.roles if patch.roles is None else patch.roles,
                before.allow if patch.allow is None else patch.allow,
                before.deny if patch.deny is None else patch.deny,
            )
            await update_agent_rules_in(s, agent_id, rules, actor, f"update rules of agent {fields.name}")
            agent.name, agent.description, agent.model = fields.name, fields.description, fields.model
            agent.thinking = fields.thinking
            agent.group = fields.group
            await s.flush()
            return info_of(agent)

    async def set_default(self, agent_id: str, is_default: bool) -> AgentInfo:
        """设为（或取消）系统默认 agent。设为默认时同一事务里先清掉旧的，始终最多一个；取消不是默认的 agent 不做任何事。
        归档的不能设为默认（新对话会默认选中一个不能运行的 agent）。"""
        async with self._db.transaction() as s:
            agent = await (runnable_row_in if is_default else active_row_in)(s, agent_id)
            if is_default:
                await s.execute(
                    update(Agent).where(Agent.is_default, Agent.id != agent_id).values(is_default=False)
                )
            agent.is_default = is_default
            await s.flush()
            return info_of(agent)

    def _validated(self, agent: Agent, patch: AgentPatch) -> _AgentFields:
        """patch 合并到 agent 当前值之后的字段（分发规则之外），校验过（不合法时报 InvalidInput）。"""
        fields = _AgentFields(
            name=agent.name if patch.name is None else new_agent_name(patch.name),
            description=agent.description if patch.description is None else patch.description,
            model=agent.model if patch.model is None else patch.model,
            thinking=cast(Thinking, agent.thinking) if patch.thinking is None else patch.thinking,
            group=agent.group if patch.group is None else normalize_group(patch.group),
        )
        self._check(fields)
        return fields

    async def get_active(self, agent_id: str) -> AgentInfo:
        """还可用的 agent：正在删除的按不存在处理（例如读它的记忆、条目时）。为它写数据的检查在写入的地方：数据库数据在
        同一事务里用 active_agent_in。"""
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
            return [info_of(a) for a in await _listed_in(s)]

    async def list_with_rules(self) -> list[tuple[AgentInfo, AgentRules]]:
        """list 加上各自的分发规则（分发子域的 all_agent_rules_in），同一个事务里读：两边一致，缺规则即数据损坏、报错。"""
        async with self._db.transaction() as s:
            rules = await all_agent_rules_in(s)
            return [(info_of(a), rules[a.id]) for a in await _listed_in(s)]

    async def _ids(self) -> set[str]:
        """数据库里所有 agent 的 id，正在删除的也算（它的目录还在是正常的，删除流程会删）。"""
        async with self._db.transaction() as s:
            return set((await s.scalars(select(Agent.id))).all())

    async def remove_workspace(self, agent_id: str) -> None:
        """删除 agent 的工作目录（删除 agent 的流程里；它的条目与记忆随数据库行删除）。"""
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
