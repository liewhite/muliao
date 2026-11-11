"""触发器：具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件（见 models.Trigger）。

触发方式是多态的，分两部分实现，新增一种方式（例如消息队列）只需各加一个实现并在 triggers.assembly.TriggerDomain.build 里注册：
- TriggerKind：纯逻辑——校验配置、推导运行状态，注册给 TriggerService；
- 激活机制：怎么被触发——到期扫描循环（due.DueRunner，定时与一次性共用）、流式托管（stream.StreamRunner）……都调用
  TriggerService.fire_activated，组装成事件交给会话领域的统一入口。

触发先跑触发器的前置检查（若有，见 triggers.precheck）：没有事件就不调用 agent。检查在数据库事务之外执行。
用户与 agent 的「立即运行一次」走 run：那是管理操作（见它的说明），不消耗一次性触发器预定的那一次。

结束（finished_at）是所有触发方式共用的概念：这个触发器不会再触发了（一次性触发器用掉了它那一次）。已结束的
不出现在默认列表、不能再修改启用或立即运行，但行与 id 保留，它发起过的会话因此仍查得到是谁发的。

每次触发都新开触发器自己的 agent 的一个会话。触发投的信件都没有用户在等（awaited = False），处理它的运行不推送，
需要通知时由 agent 调用 notify_user。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any, Literal, Protocol, assert_never
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import (
    AgentArchived,
    Availability,
    agent_in,
    availability_in,
    runnable_agent_in,
)
from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database, utcnow, when_committed_in
from muliao.platform.tasks import Clock
from muliao.platform.values import KEEP, Keep, local_time
from muliao.sessions.api import (
    Accepted,
    NewConversation,
    Refused,
    SenderInbox,
    SenderMail,
    SessionActivity,
    SessionState,
    TriggerOrigin,
    WorkState,
)
from muliao.triggers.faults import Faults, FaultSource, TriggerFault, faults_of, reset
from muliao.triggers.precheck import (
    Precheck,
    PrecheckFailed,
    PrecheckRunner,
    current_in,
    decide,
    failed,
    passed,
    precheck_alert,
    precheck_of,
    set_precheck,
)
from muliao.triggers.tables import Trigger
from muliao.workspace.api import AgentCommands

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# 事件内容原样进入 prompt（流式命令的输出、前置检查的输出），过大的应由来源裁剪或改为传引用
EVENT_MAX_BYTES = 256 * 1024

# 触发器开出的会话（Origin.TRIGGER）在指令里的说明（会话级、不变）。TriggerDomain.origin_notes 交给会话领域（sessions.api.OriginNotes）
SESSION_NOTE = (
    "This run was started by a trigger (a schedule, or an external event shown inside <event>); nobody is "
    "watching. Each firing starts a fresh conversation, so keep anything later firings need in memory or "
    "workspace files."
)


@dataclass(frozen=True)
class TriggerView:
    """触发器的读模型（HTTP 层与工具共用）：行、运行状态（这种方式自己的类型）、前置检查、脚本出错，上一次触发开出的会话
    （已删除时为 None，问会话领域 SessionActivity.states_of_in）。"""

    trigger: Trigger
    status: BaseModel
    precheck: Precheck | None
    faults: list[TriggerFault]
    last_session_id: str | None


class _By(Enum):
    """谁在触发：决定前置检查失败怎么处理。"""

    # 用户或 agent 的「立即运行一次」：只报给调用方，不记不告警（见 run）
    RUN = "run"
    # 激活机制（到期扫描、流式）：没人等结果，记下并告警（见 fire_activated）
    ACTIVATION = "activation"


@dataclass(frozen=True)
class Fired:
    """一次触发投出去的信：新开的会话（id 预先分配，投递之后通常毫秒级出现），触发器记成 last_session_id。"""

    session_id: str


class Skip(StrEnum):
    """触发器自己的原因让这次没触发。"""

    # 已停用
    DISABLED = "disabled"
    # 已删除，或它的 agent 正在删除（触发器随之删除，on_agent_deleting）
    DELETED = "deleted"
    # 它的 agent 已归档：触发器保持启用，取消归档后照常触发
    ARCHIVED = "archived"


@dataclass(frozen=True)
class Skipped:
    why: Skip
    detail: str


@dataclass(frozen=True)
class CheckFailed:
    """前置检查失败（自动触发时已记成出错并告警，见 _fire）。"""

    error: PrecheckFailed


# 一次触发的结果：投出去了、这次没事（前置检查判定不触发，为 None）、触发器自己不触发、检查失败
type Outcome = Fired | None | Skipped | CheckFailed


def _settled(outcome: Outcome) -> Fired | None:
    """在等结果的调用方（立即运行）：没投出去的原因按种类报错。"""
    match outcome:
        case Fired() | None:
            return outcome
        case Skipped(why=Skip.DISABLED, detail=detail):
            raise Conflict(detail)
        case Skipped(why=Skip.ARCHIVED, detail=detail):
            raise AgentArchived(detail)
        case Skipped(why=Skip.DELETED, detail=detail):
            raise TriggerNotFound(detail)
        case Skipped(why=other):
            assert_never(other)
        case CheckFailed(error=error):
            raise error
        case other:
            assert_never(other)


# 全部触发方式的名字（各方式模块的 KIND）；接口契约里的种类就是它，与 TriggerDomain.build 注册的方式一一对应（tests/test_triggers.py）
TriggerKindName = Literal["schedule", "once", "stream"]


class TriggerKind(Protocol):
    """一种触发方式的纯逻辑部分。"""

    name: str
    # 给模型与界面看的说明：什么时候触发、config 的格式
    description: str

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """校验并规范化 config，返回 (config, 由配置推导的 state)。

        在创建、修改配置（规范化后与原配置相同的不算）、重新启用时调用；TriggerService 把推导结果合并进已有 state，
        所以实现不必负责保留与配置无关的部分（如流式的运行状态）。
        """
        ...

    def secrets(self, config: dict[str, Any]) -> list[str]:
        """规范化后的 config 交给命令的 vault key（没有就是空列表）；创建、改配置、重新启用时据此核对 secret 可用。"""
        ...

    def status(self, trigger: Trigger) -> BaseModel:
        """给界面与模型看的运行状态（如下次触发时间），不含任何凭证。

        每种方式有自己的状态类型；HTTP 接口按 kind 区分的联合类型（api/triggers.py）列出全部方式。
        """
        ...


def validate_trigger_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise InvalidInput(f"invalid trigger name {name!r}: must match {_NAME.pattern}")
    return name


def _finished(name: str) -> Conflict:
    return Conflict(
        f"trigger {name!r} has finished and will not fire again; it can only be viewed or deleted"
    )


def event_prompt(instructions: str, payload: str | None) -> str:
    if payload is None:
        return instructions
    return f"{instructions.strip()}\n\n<event>\n{payload}\n</event>"


def parse_config[M: BaseModel](kind: str, model: type[M], config: dict[str, Any]) -> M:
    """按这种方式的配置类型（同时是 HTTP 接口里的 config 类型，extra=forbid）校验 config，不合法时报 InvalidInput。"""
    try:
        return model.model_validate(config)
    except ValidationError as e:
        raise InvalidInput(f"invalid {kind} trigger config {config!r}: {e}") from e


async def trigger_in(s: AsyncSession, trigger_id: int) -> Trigger:
    """在调用方事务里取这一行（触发器领域内部用，不公开：HTTP 层与别的领域经 TriggerService 的命令与查询）。"""
    trigger = await s.get(Trigger, trigger_id)
    if trigger is None:
        raise TriggerNotFound(f"trigger {trigger_id} not found")
    return trigger


class TriggerNotFound(NotFound):
    """触发器不存在（已删除）。"""


async def _skip_in(s: AsyncSession, trigger: Trigger) -> Skipped | None:
    """触发器自己能不能触发：停用、它的 agent 已归档或正在删除（触发器随之删除，按已删除）时这次不触发。投递之前在同一事务里
    查，投递投不进就都是目标的问题。"""
    if not trigger.enabled:
        return Skipped(Skip.DISABLED, f"trigger {trigger.name!r} is disabled")
    match await availability_in(s, trigger.agent_id):
        case Availability.RUNNABLE:
            return None
        case Availability.ARCHIVED:
            name = (await agent_in(s, trigger.agent_id)).name
            return Skipped(
                Skip.ARCHIVED, f"trigger {trigger.name!r}: agent {name!r} is archived; unarchive it first"
            )
        case Availability.GONE:
            return Skipped(Skip.DELETED, f"trigger {trigger.name!r}: its agent is being deleted")
        case other:
            assert_never(other)


def set_state(trigger: Trigger, **values: Any) -> None:
    """激活机制维护自己的运行状态（触发器领域内部用，在调用方事务里改这一行）。JSON 列只在整体赋值时被追踪。"""
    trigger.state = {**trigger.state, **values}


async def previous_fire_state_in(
    s: AsyncSession, activity: SessionActivity, trigger: Trigger
) -> SessionState:
    """上一次触发开出的会话现在的工作状态（会话领域回答；会话已删除时 session_id 为 None），从没触发过时是
    idle。不是 idle 就还有没做完的事：在运行或排着运行，或在等后台任务、委派、到点唤醒、用户回答——
    等待中的会话结果回来会自动接着做，再开一个会重叠（用户 11-11 定，#627）。定时触发据此跳过，流式触发据此让事件排队
    （触发器领域内部用）。"""
    if trigger.last_session_id is None:
        return SessionState(WorkState.IDLE, None)
    [state] = await activity.states_of_in(s, [trigger.last_session_id])
    return state


def finish_in(trigger: Trigger, now: datetime) -> None:
    """结束：这个触发器不会再触发了（一次性触发器用掉了它那一次），在调用方的事务里改这一行（触发器领域内部用）。

    结束之后它不出现在默认列表里，也不能再修改、启用或立即运行；行保留着，它发起过的会话因此仍查得到
    是谁发的，同名的触发器也可以重新创建。
    """
    trigger.finished_at = now
    log.info("trigger finished", extra={"trigger": trigger.name, "kind": trigger.kind})


class TriggerService:
    def __init__(
        self,
        db: Database,
        kinds: Mapping[str, TriggerKind],
        tz: ZoneInfo,
        commands: AgentCommands,
        precheck: PrecheckRunner,
        inbox: SenderInbox,
        faults: Faults,
        activity: SessionActivity,
        clock: Clock = utcnow,
    ):
        self._activity = activity
        self._commands = commands
        self._precheck = precheck
        self._inbox = inbox
        self._faults = faults
        self._db = db
        self.kinds = dict(kinds)
        self._tz = tz
        self._clock = clock
        # 每个激活机制各自订阅变化通知（见 watch），互不清除对方的通知
        self._subscribers: list[asyncio.Event] = []

    async def on_agent_deleting(self, s: AsyncSession, event: AgentDeleting) -> None:
        """删除 agent 的流程：删除它的触发器。"""
        await s.execute(delete(Trigger).where(Trigger.agent_id == event.agent_id))

        async def changed() -> None:
            self._changed()

        when_committed_in(s, changed)

    def watch(self) -> asyncio.Event:
        """返回一个只属于调用方的 Event：任何触发器变化时置位，由调用方自己清除。"""
        event = asyncio.Event()
        self._subscribers.append(event)
        return event

    def _changed(self) -> None:
        for event in self._subscribers:
            event.set()

    def kind(self, name: str) -> TriggerKind:
        if name not in self.kinds:
            raise InvalidInput(f"unknown trigger kind {name!r}; available: {sorted(self.kinds)}")
        return self.kinds[name]

    async def views(self, triggers: list[Trigger]) -> list[TriggerView]:
        """触发器的读模型：行、它的触发方式给出的运行状态（TriggerKind.status）、解析好的前置检查与脚本出错，以及上一次
        触发开出的会话还在不在（问会话领域，一个事务）。"""
        fired = [t for t in triggers if t.last_session_id is not None]
        async with self._db.transaction() as s:
            states = await self._activity.states_of_in(
                s, [t.last_session_id for t in fired if t.last_session_id is not None]
            )
        last = {t.id: state.session_id for t, state in zip(fired, states, strict=True)}
        sessions = [last.get(t.id) for t in triggers]
        return [
            TriggerView(t, self.kind(t.kind).status(t), precheck_of(t), faults_of(t), session)
            for t, session in zip(triggers, sessions, strict=True)
        ]

    async def view(self, trigger: Trigger) -> TriggerView:
        return (await self.views([trigger]))[0]

    async def _check_secrets(self, agent_id: str, keys: list[str]) -> None:
        """前置检查与流式命令都是 agent 自己写的命令：创建或修改时就确认这些 secret 能交给命令（含 allow_shell），
        配置错误在第一现场报出，而不是等到运行时一直失败。与运行时起命令是同一步（AgentCommands.prepare），核对与实际
        不会分叉。要读 vault（它自己开事务），不能在别的事务里调用。"""
        await self._commands.prepare(agent_id, keys, shell=True)

    async def create(
        self,
        *,
        name: str,
        agent_id: str,
        kind: str,
        prompt: str,
        config: dict[str, Any],
        precheck: Precheck | None = None,
    ) -> Trigger:
        if not prompt.strip():
            raise InvalidInput("trigger prompt must not be empty")
        trigger_kind = self.kind(kind)
        config, state = trigger_kind.configure(config)
        await self._check_secrets(
            agent_id, trigger_kind.secrets(config) + (precheck.secrets if precheck is not None else [])
        )
        trigger = Trigger(
            name=validate_trigger_name(name),
            agent_id=agent_id,
            prompt=prompt,
            kind=kind,
            config=config,
            state=state,
            enabled=True,
        )
        set_precheck(trigger, precheck)
        try:
            async with self._db.transaction() as s:
                # 正在删除的 agent 按不存在处理：否则删除流程清理过之后又建的触发器没人认领；归档的拒绝（它不能运行）
                await runnable_agent_in(s, agent_id)
                s.add(trigger)
        except IntegrityError as e:
            raise Conflict(f"trigger {name!r} already exists") from e
        self._changed()
        log.info("trigger created", extra={"trigger": name, "kind": kind, "agent": agent_id})
        return trigger

    def _rederive(
        self, trigger: Trigger, config: dict[str, Any] | None, enabled: bool | None
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """这次修改要不要按 TriggerKind.configure 重新推导，要就返回 (config, 推导出的 state)。

        配置变了、或停用后重新启用（如定时触发器的下次时间要从现在算起、流式触发重起进程）才重新推导；规范化后没变的
        配置（界面只改说明时也带着原配置）不算改动，运行状态留着。"""
        reenabled = enabled is True and not trigger.enabled
        if config is None and not reenabled:
            return None
        derived_config, derived = self.kind(trigger.kind).configure(
            trigger.config if config is None else config
        )
        if reenabled or derived_config != trigger.config:
            return derived_config, derived
        return None

    async def update(
        self,
        trigger_id: int,
        *,
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        precheck: Precheck | None | Keep = KEEP,
    ) -> Trigger:
        """在一个事务里只修改给出的字段（None 不变；precheck 为 KEEP 不变、None 去掉）。
        重新启用时清除停用的原因；换前置检查、改配置或重新启用是人工干预，清掉脚本的出错（faults.reset），再出错重新告警。"""
        if prompt is not None and not prompt.strip():
            raise InvalidInput("trigger prompt must not be empty")
        if isinstance(precheck, Precheck) or config is not None or enabled is True:
            # 核对 secret 要读 vault，不能放在下面的事务里：先按此刻的行算出会生效的配置。中间若有并发修改，核对按的是
            # 旧行，后果只是走回运行时的失败路径（与建好后 secret 才被删一样），有意不加锁
            async with self._db.transaction() as s:
                current = await self._manageable_in(s, trigger_id)
                agent_id = current.agent_id
                keys = precheck.secrets if isinstance(precheck, Precheck) else []
                if (rederived := self._rederive(current, config, enabled)) is not None:
                    keys = keys + self.kind(current.kind).secrets(rederived[0])
            await self._check_secrets(agent_id, keys)
        async with self._db.transaction() as s:
            trigger = await self._manageable_in(s, trigger_id)
            if not isinstance(precheck, Keep):
                set_precheck(trigger, precheck)
                reset(trigger)
            if prompt is not None:
                trigger.prompt = prompt
            reenabled = enabled is True and not trigger.enabled
            if (rederived := self._rederive(trigger, config, enabled)) is not None:
                derived_config, derived = rederived
                # JSON 列只在整体赋值时被追踪
                trigger.config, trigger.state = derived_config, {**trigger.state, **derived}
                reset(trigger)  # 配置（如流式命令）换了或重新启用
            if enabled is not None:
                trigger.enabled = enabled
                if reenabled:
                    trigger.disabled_reason = None
        self._changed()
        log.info("trigger updated", extra={"trigger": trigger.name, "enabled": trigger.enabled})
        return trigger

    async def disable(self, trigger_id: int, reason: str) -> bool:
        """系统停用触发器（如按到期触发失败），原因记在 disabled_reason 给用户看；用户重新启用时清除。
        触发器已被删除时返回 False。"""
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            if trigger is None:
                return False
            trigger.enabled = False
            trigger.disabled_reason = reason
        self._changed()
        return True

    async def delete(self, trigger_id: int) -> None:
        async with self._db.transaction() as s:
            trigger = await trigger_in(s, trigger_id)
            await s.delete(trigger)
        self._changed()
        log.info("trigger deleted", extra={"trigger": trigger.name, "trigger_id": trigger_id})

    async def list(
        self, *, kind: str | None = None, enabled_only: bool = False, finished: bool = False
    ) -> list[Trigger]:
        """默认只列还会触发的（按名称）；finished 为真时只列已结束的历史（按结束时间倒序）。"""
        query = (
            select(Trigger).where(Trigger.finished_at.isnot(None)).order_by(Trigger.finished_at.desc())
            if finished
            else select(Trigger).where(Trigger.finished_at.is_(None)).order_by(Trigger.name)
        )
        if kind is not None:
            query = query.where(Trigger.kind == kind)
        if enabled_only:
            query = query.where(Trigger.enabled)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def _manageable_in(self, s: AsyncSession, trigger_id: int) -> Trigger:
        """管理操作（修改、启用、立即运行）的前提：这个触发器还会触发。已结束的只能查看和删除。"""
        trigger = await trigger_in(s, trigger_id)
        if trigger.finished_at is not None:
            raise _finished(trigger.name)
        return trigger

    async def get_by_name(self, name: str) -> Trigger:
        """按名称找还会触发的那个：名称只在未结束的触发器之间唯一，已结束的只能按 id 指。

        这个名字只剩已结束的触发器时说清楚它已结束，而不是「找不到」：它在已结束列表里按名字看得见。
        """
        async with self._db.transaction() as s:
            same_name = (await s.scalars(select(Trigger).where(Trigger.name == name))).all()
            live = [t for t in same_name if t.finished_at is None]
            if live:
                return live[0]
            if same_name:
                # 按名称的操作（含删除）都指不到已结束的：别提示「可以删除」，说清楚它已是历史、无需清理
                raise Conflict(
                    f"trigger {name!r} has finished and will not fire again; it is kept as history "
                    "(see trigger_list(finished=true)), so there is nothing to change, run or delete"
                )
            raise NotFound(f"trigger {name!r} not found")

    async def _deliver_in(self, s: AsyncSession, trigger: Trigger, payload: str | None) -> Fired:
        """新开触发器的 agent 的一个会话交出这次事件（只由 _fire 调用）：记下触发与投信在同一事务，恰好一次。payload 为
        事件内容（无则只有处理说明）。"""
        mail = SenderMail(
            sender=TriggerOrigin(trigger_id=trigger.id),
            content=event_prompt(trigger.prompt, payload),
            session=NewConversation(
                agent_id=trigger.agent_id,
                title=f"{trigger.name} · {local_time(self._clock(), self._tz)}",
            ),
        )
        match await self._inbox.receive_in(s, mail):
            case Accepted(session_id=session_id):
                pass
            case Refused(detail=detail):
                # 新会话只看 agent 能不能运行，_fire 在同一事务里刚由 _skip_in 确认过
                raise RuntimeError(f"trigger {trigger.name!r}: own agent refused after the check: {detail}")
            case other:
                assert_never(other)
        trigger.last_fired_at = self._clock()
        # 定时跳过、流式排队问它忙不忙（previous_fire_state_in）
        trigger.last_session_id = session_id
        log.info(
            "trigger fired", extra={"trigger": trigger.name, "kind": trigger.kind, "session_id": session_id}
        )
        return Fired(session_id=session_id)

    async def run(self, trigger_id: int) -> Fired | None:
        """用户或 agent 的「立即运行一次」（不带事件内容）：管理操作，不消耗一次性触发器预定的那一次。

        有前置检查时同样先跑检查，可以用来测试整条链路；返回投出去的信，检查判定没事时为 None。检查失败只报给
        调用方（PrecheckFailed），不记在触发器上、不告警：agent 修脚本时用它验证，告警会另开会话重复处理；记下了又会压住之后
        自动触发时的告警（只在新出现时告警），用户点了运行看到报错，脚本的主人却永远收不到。没投出去的其余原因按种类报错
        （_settled）。
        """
        async with self._db.transaction() as s:
            await self._manageable_in(s, trigger_id)
        return _settled(await self._fire(trigger_id, None, _By.RUN))

    async def fire_activated(self, trigger_id: int, payload: str | None = None) -> None:
        """激活机制（到期扫描、流式）的触发：没有调用方等着结果，按结果逐种处理。

        这是激活机制的入口，不看触发器是否已结束（finished_at）：按到期时间触发的方式（triggers.due）在调用前就把这次到期
        消耗掉了，一次性触发器那时已经结束。
        - 前置检查失败：已记成出错并告警；脚本的故障往往是暂时的，保持启用；
        - 它的 agent 已归档：跳过，触发器保持启用；一次性触发器那一次已用掉，告警说任务没做（agent 收不到，退给用户）；
        - 被停用或删除：不是触发失败，只记日志，不覆盖失败原因。"""
        match await self._fire(trigger_id, payload, _By.ACTIVATION):
            case Fired() | None | CheckFailed():
                pass
            case Skipped(why=Skip.ARCHIVED, detail=detail):
                log.info("activated fire skipped: %s", detail, extra={"trigger_id": trigger_id})
                await self._once_skipped(trigger_id)
            case Skipped(why=Skip.DISABLED | Skip.DELETED, detail=detail):
                log.info("activated fire skipped: %s", detail, extra={"trigger_id": trigger_id})
            case Skipped(why=other):
                assert_never(other)
            case other:
                assert_never(other)

    async def _once_skipped(self, trigger_id: int) -> None:
        """一次性触发器已用掉它那一次（到期时已结束），任务没做：告警。定时与流式的只是跳过这一次，不告警。"""
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            if trigger is None or trigger.finished_at is None:
                return
            await self._faults.alert_in(
                s,
                trigger,
                title="未执行：它的 agent 已归档",
                letter=(
                    f"Your one-off trigger `{trigger.name}` came due while you were archived, so its task was NOT "
                    f"done and it has finished. Its prompt was:\n<prompt>\n{trigger.prompt}\n</prompt>"
                ),
                summary=trigger.prompt,
            )

    async def _fire(self, trigger_id: int, payload: str | None, by: _By) -> Outcome:
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, trigger_id)
            if trigger is None:
                return Skipped(Skip.DELETED, f"trigger {trigger_id} not found")
            # 停用、归档的 agent 不跑前置检查、不触发；检查结束后投递前再查一次（检查期间可能被归档）
            if (skipped := await _skip_in(s, trigger)) is not None:
                return skipped
            if trigger.precheck is None:
                return await self._deliver_in(s, trigger, payload)
            name, agent_id, stored = trigger.name, trigger.agent_id, trigger.precheck
        precheck = Precheck.model_validate(stored)

        try:
            output = await self._precheck.run(agent_id, precheck, payload)
        except PrecheckFailed as e:
            if by is not _By.RUN:
                await self._precheck_failed(trigger_id, stored, str(e))
            return CheckFailed(e)

        async with self._db.transaction() as s:
            trigger = await current_in(s, trigger_id, stored)
            if trigger is None:
                return None
            if (skipped := await _skip_in(s, trigger)) is not None:
                return skipped
            try:
                decision = decide(precheck, output, trigger.precheck_output, self._precheck.max_bytes)
            except PrecheckFailed as e:
                failure = e
            else:
                if decision.event is None:
                    passed(trigger, precheck, output, self._clock())
                    log.debug(
                        "trigger precheck: nothing to do, not firing",
                        extra={"trigger": name, "baseline": decision.baseline},
                    )
                    return None
                passed(trigger, precheck, output, self._clock())
                return await self._deliver_in(s, trigger, decision.event)
        # 判定本身失败（差异过大）：在这个事务之外记录
        if by is not _By.RUN:
            await self._precheck_failed(trigger_id, stored, str(failure))
        return CheckFailed(failure)

    async def _precheck_failed(self, trigger_id: int, checked: dict[str, Any], error: str) -> None:
        """记下检查失败（检查期间触发器变了就作废，current_in），从正常变为出错时告警。"""
        async with self._db.transaction() as s:
            trigger = await current_in(s, trigger_id, checked)
            if trigger is None:
                return
            failed(trigger, self._clock())
            await self._faults.report_in(
                s,
                trigger,
                [TriggerFault(source=FaultSource.PRECHECK, error=error)],
                lambda _new: precheck_alert(trigger, error),
            )
