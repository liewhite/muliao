"""agent 目录（子域）：agent 的身份与配置及其工作目录。身份（id）、归档、默认 agent、删除标记在 agents 表；名字、描述、模型、
思考档位、分组的事实来源是资源目录（`<名>/agent.toml` 与目录名，agents.assets），表里那几列是同步来的镜像
（agents.source）。新建与修改都写文件（agent.toml、access.toml 里它的规则、空的私有记忆）再写表；改名就是挪目录。删除与
归档的流程在 agents.lifecycle，它对 agents 表的写入用这里的 `*_in`。"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

import tomlkit
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents import access_file, assets
from muliao.agents.access import grants_for
from muliao.agents.active import AgentNames, active_row_in, agent_names_in, runnable_row_in
from muliao.agents.agent_rules import AgentRules
from muliao.agents.assets import AGENT_FILE, PLATFORM, PRIVATE_MEMORY_FILE, Snapshot
from muliao.agents.distribution import check_agent_rules
from muliao.agents.ids import new_agent_id
from muliao.agents.kinds import StoredKind
from muliao.agents.naming import new_agent_name, normalize_agent_name
from muliao.agents.source import AssetSource
from muliao.agents.tables import Agent
from muliao.agents.thinking import THINKING_LEVELS, Thinking, check_thinking_level
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database, utcnow

log = logging.getLogger(__name__)

__all__ = ["THINKING_LEVELS", "Thinking"]

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

    def given(self) -> frozenset[str]:
        """给出了（要改）的字段名。"""
        return frozenset(f.name for f in dataclasses.fields(self) if getattr(self, f.name) is not None)


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
        # 同步镜像前已校验（agents.source）
        thinking=cast(Thinking, agent.thinking),
        created_at=agent.created_at,
        is_default=agent.is_default,
        group=agent.group,
        archived_at=agent.archived_at,
    )


async def active_agent_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """还可用的 agent（正在删除的按不存在处理）。别的领域在自己的写事务里为某个 agent 写数据之前用它检查：事务串行，
    agent 只会从可用变为正在删除、再变为不存在，检查与写入之间不会插进删除，删除流程的清理一定看得到这次写入。"""
    return info_of(await active_row_in(s, agent_id))


async def runnable_agent_in(s: AsyncSession, agent_id: str) -> AgentInfo:
    """能运行的 agent：还可用且没有归档，归档的报 AgentArchived（Conflict）。给它派新工作的入口用它：来信与新会话
    （sessions.inbound）、指派与 owner（issues）、触发器的创建与触发。管理与资源的写入（改配置、记忆、secret）用 active_agent_in，
    归档的照常可改。"""
    return info_of(await runnable_row_in(s, agent_id))


async def named_agent_in(s: AsyncSession, name: str) -> AgentInfo:
    """按名称找还可用的 agent（模型与脚本按名称指代 agent，不接触 id；名称唯一）。没有时 NotFound，列出可用的名称。"""
    return info_of(await _named_row_in(s, name))


async def _named_row_in(s: AsyncSession, name: str) -> Agent:
    name = normalize_agent_name(name)
    agent = await s.scalar(select(Agent).where(Agent.name == name, Agent.deleting_by.is_(None)))
    if agent is None:
        names = (
            await s.scalars(select(Agent.name).where(Agent.deleting_by.is_(None)).order_by(Agent.name))
        ).all()
        raise NotFound(f"no agent named {name!r}; available agents: {list(names)}")
    return agent


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
    """正在删除的 agent 的发起删除的人（actor）；行已删除时 None（删除流程重放）。没在删除中说明流程与数据不一致，报错。"""
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
        source: AssetSource,
        workspaces_dir: Path,
        # (模型, 思考档位) 在当前配置下不能运行时报 InvalidInput（agent_runtime.llm.ModelRegistry.check）
        model_checker: Callable[[str, Thinking], None],
        defaults: AgentDefaults,
    ):
        self._db = db
        self._source = source
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
                check_thinking_level(agent.thinking)
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
        """不依赖快照的校验；分发规则在写入时按快照校验（distribution.check_agent_rules）。"""
        check_thinking_level(fields.thinking)
        if not fields.description.strip():
            raise InvalidInput("agent description must not be empty")
        self._check_model(fields.model, fields.thinking)

    @staticmethod
    async def _check_name_free(s: AsyncSession, snapshot: Snapshot, name: str, agent_id: str | None) -> None:
        """名字要没人用：数据库里别的 agent（含目录坏掉、只剩镜像的）、资源目录里已有的目录、同名的共享记忆（私有记忆名就是
        agent 名）。只给名称：这条错误也会交还给模型，模型不接触 id。"""
        holder = await s.scalar(select(Agent.id).where(Agent.name == name))
        if holder is not None and holder != agent_id:
            raise Conflict(f"agent name {name!r} is already taken by another agent")
        taken = {p.name.casefold() for p in snapshot.root.iterdir()} if snapshot.root.is_dir() else set()
        own = snapshot.agents.get(agent_id) if agent_id else None
        if name.casefold() in taken - ({own.path.name.casefold()} if own else set()) or name == PLATFORM:
            raise Conflict(f"agent name {name!r} is already taken by a directory in {snapshot.root}")
        memory = snapshot.get(StoredKind.MEMORY, name)
        if memory is not None and memory.owner != agent_id:
            raise Conflict(
                f"a memory named {name!r} already exists (private memories carry the agent's name)"
            )

    async def create(self, spec: AgentSpec, *, actor: str) -> AgentInfo:
        """actor：创建者，"user" 或创建它的 agent 的 id。建资源目录（agent.toml、空的私有记忆）、写它的规则、插行、建工作目录。"""
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
        rules = AgentRules.of(spec.roles, spec.allow, spec.deny)
        async with self._source.writing() as snapshot:
            check_agent_rules(snapshot, rules)
            async with self._db.transaction() as s:
                await self._check_name_free(s, snapshot, name, None)
            directory = snapshot.root / name
            await asyncio.to_thread(
                _write_new_agent, directory, snapshot.access_file, agent_id, fields, rules
            )
            try:
                async with self._db.transaction() as s:
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
                    self.workspace(agent_id).mkdir(parents=True, exist_ok=True)
                    info = info_of(agent)
            except BaseException:
                await asyncio.to_thread(_undo_new_agent, directory, snapshot.access_file, name)
                raise
        log.info("agent created", extra={"agent": agent_id, "name": name, "actor": actor})
        return info

    async def check_update(self, agent_id: str, fields: frozenset[str], *, actor: str) -> None:
        """actor 能不能改这个 agent 的这些字段（access.Grants.check_agent_update，不能时 Forbidden）。"""
        grants_for(await self._source.snapshot(), actor).check_agent_update(agent_id, fields)

    async def update(self, agent_id: str, patch: AgentPatch, *, actor: str) -> AgentInfo:
        """只修改给出的字段：配置改 agent.toml（保留人手写的注释），规则改 access.toml，改名挪目录（access.toml 里那一节跟着
        改名），再同步镜像。actor 不是用户也不是 admin 时只能改自己的一部分字段（access.Grants.check_agent_update）。"""
        async with self._source.writing() as snapshot:
            grants_for(snapshot, actor).check_agent_update(agent_id, patch.given())
            async with self._db.transaction() as s:
                agent = await active_row_in(s, agent_id)
                fields = self._validated(agent, patch)
                await self._check_name_free(s, snapshot, fields.name, agent_id)
            directory = snapshot.agents.get(agent_id)
            if directory is None:
                raise NotFound(
                    f"agent {agent.name!r} has no directory with its {AGENT_FILE} in {snapshot.root}; fix that first"
                )
            before = snapshot.rules_of(agent_id)
            rules = AgentRules.of(
                before.roles if patch.roles is None else patch.roles,
                before.allow if patch.allow is None else patch.allow,
                before.deny if patch.deny is None else patch.deny,
            )
            check_agent_rules(snapshot, rules, before)
            await asyncio.to_thread(
                _write_agent_update, directory.path, snapshot.access_file, directory.name, fields, rules
            )
        await self._source.snapshot()
        async with self._db.transaction() as s:
            return info_of(await active_row_in(s, agent_id))

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

    async def get_with_rules(self, agent_id: str) -> tuple[AgentInfo, AgentRules]:
        """还可用的 agent 加上它的分发规则。"""
        snapshot = await self._source.snapshot()
        return await self.get_active(agent_id), snapshot.rules_of(agent_id)

    async def get(self, agent_id: str) -> AgentInfo:
        """agent（包括正在删除的）。"""
        async with self._db.transaction() as s:
            return await agent_in(s, agent_id)

    async def find_by_name(self, name: str) -> AgentInfo:
        """模型按名称指代 agent（不接触 id）：在工具层把名称解析为 agent。名称唯一。"""
        async with self._db.transaction() as s:
            return await named_agent_in(s, name)

    async def find_with_rules(self, name: str) -> tuple[AgentInfo, AgentRules]:
        """按名称找到的 agent 加上它的分发规则，同一个事务里读（get_agent 工具）。"""
        snapshot = await self._source.snapshot()
        async with self._db.transaction() as s:
            agent = await named_agent_in(s, name)
        return agent, snapshot.rules_of(agent.id)

    async def names(self) -> AgentNames:
        async with self._db.transaction() as s:
            return await agent_names_in(s)

    async def list(self) -> list[AgentInfo]:
        async with self._db.transaction() as s:
            return [info_of(a) for a in await _listed_in(s)]

    async def list_with_rules(self) -> list[tuple[AgentInfo, AgentRules]]:
        """list 加上各自的分发规则。"""
        snapshot = await self._source.snapshot()
        return [(a, snapshot.rules_of(a.id)) for a in await self.list()]

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


def _agent_toml(directory: Path, agent_id: str, fields: _AgentFields) -> str:
    """agent.toml 的新内容：已有的文件在原文上改（保留注释与别的排版），没有就新建。"""
    path = directory / AGENT_FILE
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    doc["id"] = agent_id
    doc["description"] = fields.description
    doc["model"] = fields.model
    doc["thinking"] = fields.thinking
    if fields.group is None:
        doc.pop("group", None)
    else:
        doc["group"] = fields.group
    return tomlkit.dumps(doc)


def _write_new_agent(
    directory: Path, access: Path, agent_id: str, fields: _AgentFields, rules: AgentRules
) -> None:
    directory.mkdir(parents=True)
    assets.write_text(directory / AGENT_FILE, _agent_toml(directory, agent_id, fields))
    assets.write_text(directory / PRIVATE_MEMORY_FILE, "")
    access_file.edit(access, lambda doc: access_file.set_agent_rules(doc, fields.name, rules))


def _undo_new_agent(directory: Path, access: Path, name: str) -> None:
    assets.remove(directory)
    access_file.edit(access, lambda doc: access_file.remove_agent(doc, name))


def _write_agent_update(
    directory: Path, access: Path, old_name: str, fields: _AgentFields, rules: AgentRules
) -> None:
    agent_id = tomlkit.parse((directory / AGENT_FILE).read_text(encoding="utf-8"))["id"]
    assets.write_text(directory / AGENT_FILE, _agent_toml(directory, str(agent_id), fields))

    def change(doc: tomlkit.TOMLDocument) -> None:
        if fields.name != old_name:
            access_file.rename_agent(doc, old_name, fields.name)
        access_file.set_agent_rules(doc, fields.name, rules)

    access_file.edit(access, change)
    if fields.name != old_name:
        assets.move(directory, directory.parent / fields.name)
