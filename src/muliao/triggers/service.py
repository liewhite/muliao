"""触发器：具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件（见 models.Trigger）。

触发方式是多态的，分两部分实现，新增一种方式（例如消息队列）只需各加一个实现并在组合根注册：
- TriggerKind：纯逻辑——校验配置、推导运行状态，注册给 TriggerService；
- 激活机制：怎么被触发——到期扫描循环（due.DueRunner，定时与一次性共用）、HTTP 入口（webhook.Webhooks）、
  消费循环……它们都只调用 TriggerService.fire，组装成事件交给会话领域的统一入口。

fire 先跑触发器的前置检查（若有，见 triggers.precheck）：没有事件就不调用 agent。检查在数据库事务之外执行。
用户与 agent 的「立即运行一次」走 run：那是管理操作（见它的说明），不消耗一次性触发器预定的那一次。

归档（finished_at）是所有触发方式共用的概念：这个触发器不会再触发了（一次性触发器用掉了它那一次）。归档的
不出现在默认列表、不能再修改启用或立即运行，但行与 id 保留，它发起过的会话因此仍查得到是谁发的。

触发投的信件都没有用户在等（awaited = False），处理它的运行不推送，需要通知时由 agent 调用 notify_user。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from muliao.agents.api import active_agent_in
from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, DomainError, InvalidInput, NotFound
from muliao.notifications.events import Notify
from muliao.platform.db import Database, utcnow
from muliao.platform.ids import new_id
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task
from muliao.platform.tasks import Clock, Commit
from muliao.platform.values import KEEP, Keep, local_time
from muliao.sessions.api import (
    Inbound,
    NewSession,
    Origin,
    SessionActivity,
    WorkState,
    receive_command_in,
)
from muliao.triggers.precheck import Precheck, PrecheckFailed, PrecheckRunner, decide
from muliao.triggers.tables import Trigger

log = logging.getLogger(__name__)

# 出现在 webhook 地址里
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# 事件内容原样进入 prompt（webhook 请求体、前置检查的输出），过大的应由来源裁剪或改为传引用
EVENT_MAX_BYTES = 256 * 1024


class TriggerKind(Protocol):
    """一种触发方式的纯逻辑部分。"""

    name: str
    # 给模型与界面看的说明：什么时候触发、config 的格式
    description: str

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """校验并规范化 config，返回 (config, 由配置推导的 state)。

        在创建、修改配置、重新启用时调用；TriggerService 把推导结果合并进已有 state，
        所以实现不必、也不能负责保留与配置无关的部分（如 token 摘要）。
        """
        ...

    def status(self, trigger: Trigger) -> BaseModel:
        """给界面与模型看的运行状态（如下次触发时间、地址），不含任何凭证。

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
        raise NotFound(f"trigger {trigger_id} not found")
    return trigger


def set_state(trigger: Trigger, **values: Any) -> None:
    """激活机制维护自己的运行状态（触发器领域内部用，在调用方事务里改这一行）。JSON 列只在整体赋值时被追踪。"""
    trigger.state = {**trigger.state, **values}


async def previous_fire_state_in(s: AsyncSession, activity: SessionActivity, trigger: Trigger) -> WorkState:
    """上一次触发的会话的工作状态（会话领域的定义），从没触发过时是 idle。不是 idle 就还有没做完的事：在运行、
    有还没投递的来信（包括这次的），或在等后台任务、委派、用户回答——等待中的会话结果回来会自动接着做，
    再开一个会重叠。定时触发据此跳过，流式触发据此让事件排队（触发器领域内部用）。"""
    if trigger.last_session_id is None:
        return WorkState.IDLE
    return await activity.work_state_in(s, trigger.last_session_id)


def finish_in(trigger: Trigger, now: datetime) -> None:
    """归档：这个触发器不会再触发了（一次性触发器用掉了它那一次），在调用方的事务里改这一行（触发器领域内部用）。

    归档之后它不出现在默认列表里，也不能再修改、启用或立即运行；行保留着，它发起过的会话因此仍查得到
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
        precheck: PrecheckRunner,
        messaging: Messaging,
        run_now: Callable[[int], Awaitable[None]],
        clock: Clock = utcnow,
    ):
        self._precheck = precheck
        self._messaging = messaging
        # 马上执行一个已提交的投递任务（Worker.run_now）
        self._run_now = run_now
        self._db = db
        self.kinds = dict(kinds)
        self._tz = tz
        self._clock = clock
        # 每个激活机制各自订阅变化通知（见 watch），互不清除对方的通知
        self._subscribers: list[asyncio.Event] = []

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(AgentDeleting, "triggers.agent_deleting", self.on_agent_deleting)

    async def on_agent_deleting(self, event: AgentDeleting, _delivery: Delivery) -> Commit:
        """删除 agent 的流程：删除它的触发器。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(delete(Trigger).where(Trigger.agent_id == event.agent_id))
            self._changed()

        return commit

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

    def status_of(self, trigger: Trigger) -> dict[str, Any]:
        """这个触发器的运行状态（由它的触发方式给出，见 TriggerKind.status），JSON 形式。"""
        return self.kind(trigger.kind).status(trigger).model_dump(mode="json")

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
        config, state = self.kind(kind).configure(config)
        if precheck is not None:
            await self._precheck.check(agent_id, precheck)
        trigger = Trigger(
            name=validate_trigger_name(name),
            agent_id=agent_id,
            prompt=prompt,
            kind=kind,
            config=config,
            state=state,
            enabled=True,
            precheck=None if precheck is None else precheck.model_dump(),
        )
        try:
            async with self._db.transaction() as s:
                # 正在删除的 agent 按不存在处理：否则删除流程清理过之后又建的触发器没人认领
                await active_agent_in(s, agent_id)
                s.add(trigger)
        except IntegrityError as e:
            raise Conflict(f"trigger {name!r} already exists") from e
        self._changed()
        log.info("trigger created", extra={"trigger": name, "kind": kind, "agent": agent_id})
        return trigger

    async def update(
        self,
        trigger_id: int,
        *,
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        precheck: Precheck | None | Keep = KEEP,
    ) -> Trigger:
        """在一个事务里只修改给出的字段（None 不变；precheck 为 KEEP 不变、None 去掉）。重新启用时清除上次的失败原因。"""
        if prompt is not None and not prompt.strip():
            raise InvalidInput("trigger prompt must not be empty")
        if isinstance(precheck, Precheck):
            # 校验要读 vault（它自己开事务），不能放在下面的事务里；agent 不可改，先读出来即可
            async with self._db.transaction() as s:
                agent_id = (await self._manageable_in(s, trigger_id)).agent_id
            await self._precheck.check(agent_id, precheck)
        async with self._db.transaction() as s:
            trigger = await self._manageable_in(s, trigger_id)
            if precheck is not KEEP:
                trigger.precheck = None if precheck is None else precheck.model_dump()
                # 新的检查从新的基线开始比较；旧命令的失败也不记到新配置上（新命令失败时要能再告警一次）
                trigger.precheck_output = None
                trigger.precheck_error = None
            if prompt is not None:
                trigger.prompt = prompt
            reenabled = enabled is True and not trigger.enabled
            if config is not None or reenabled:
                # 配置变了、或停用后重新启用（如定时触发器的下次时间要从现在算起），都重新推导 state
                derived_config, derived = self.kind(trigger.kind).configure(
                    trigger.config if config is None else config
                )
                # JSON 列只在整体赋值时被追踪
                trigger.config, trigger.state = derived_config, {**trigger.state, **derived}
            if enabled is not None:
                trigger.enabled = enabled
                # 重新启用只清停用的原因；前置检查的错误留着（界面一直显示它），它恢复之前不会再告警一次
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
        """默认只列还会触发的（按名称）；finished 为真时只列已归档的历史（按归档时间倒序）。"""
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
        """管理操作（修改、启用、立即运行）的前提：这个触发器还会触发。已归档的只能查看和删除。"""
        trigger = await trigger_in(s, trigger_id)
        if trigger.finished_at is not None:
            raise _finished(trigger.name)
        return trigger

    async def get_by_name(self, name: str) -> Trigger:
        """按名称找还会触发的那个：名称只在未归档的触发器之间唯一，归档的只能按 id 指。

        这个名字只剩归档的触发器时说清楚是归档了，而不是「找不到」：它在归档列表里按名字看得见。
        """
        async with self._db.transaction() as s:
            same_name = (await s.scalars(select(Trigger).where(Trigger.name == name))).all()
            live = [t for t in same_name if t.finished_at is None]
            if live:
                return live[0]
            if same_name:
                # 按名称的操作（含删除）都指不到归档的：别提示「可以删除」，说清楚它已是历史、无需清理
                raise Conflict(
                    f"trigger {name!r} has finished and will not fire again; it is archived as history "
                    "(see trigger_list(finished=true)), so there is nothing to change, run or delete"
                )
            raise NotFound(f"trigger {name!r} not found")

    async def _deliver_in(
        self, s: AsyncSession, trigger: Trigger, payload: str | None, session_id: str | None
    ) -> _Fired:
        """组装成事件交给会话领域（只由 fire 调用）。

        payload 为事件内容（无则只有处理说明）；session_id 为空时新建会话，否则续写——只能续写本触发器发起的会话。
        在这个事务里校验来信（不合规的当场报错给调用方）、记下触发，同一事务发出定向命令 Receive，由会话领域在自己的
        事务里投信：触发记下了就一定投递，投递恰好一次。新建的会话 id 在这里预先分配，投递之后（通常毫秒级）会话才
        出现。
        """
        now = self._clock()
        target = new_id() if session_id is None else session_id
        inbound = Inbound(
            origin=Origin.TRIGGER,
            origin_ref=str(trigger.id),
            content=event_prompt(trigger.prompt, payload),
            session=NewSession(
                agent_id=trigger.agent_id, title=f"{trigger.name} · {local_time(now, self._tz)}", id=target
            )
            if session_id is None
            else session_id,
            awaited=False,
        )
        delivery = self._messaging.send_in(s, await receive_command_in(s, inbound))
        trigger.last_fired_at = now
        trigger.last_session_id = target
        log.info("trigger fired", extra={"trigger": trigger.name, "kind": trigger.kind, "session_id": target})
        return _Fired(session_id=target, delivery=delivery)

    async def run(self, trigger_id: int) -> str | None:
        """用户或 agent 的「立即运行一次」（不带事件内容）：管理操作，不消耗一次性触发器预定的那一次。

        有前置检查时同样先跑检查，可以用来测试整条链路；返回交给的会话 id，检查判定没事时为 None。检查失败只报给
        调用方（PrecheckFailed），不记在触发器上、不告警：agent 修脚本时用它验证，告警会另开会话重复处理；记下了又会压住之后
        自动触发时的告警（只在正常变出错时告警），用户点了运行看到报错，脚本的主人却永远收不到。
        """
        async with self._db.transaction() as s:
            await self._manageable_in(s, trigger_id)
        return await self.fire(trigger_id, record_failure=False)

    async def fire(
        self,
        trigger_id: int,
        payload: str | None = None,
        session_id: str | None = None,
        *,
        record_failure: bool = True,
    ) -> str | None:
        """所有触发方式的唯一出口，返回交给的会话 id。有前置检查时先跑检查：不触发返回 None（这次不调用 agent）；
        检查失败记在触发器上并抛出 PrecheckFailed（触发器保持启用：脚本的故障往往是暂时的）。

        这是激活机制的入口，不看是否已归档：按到期时间触发的方式（triggers.due）在调用前就把这次到期消耗掉了，
        一次性触发器那时已经归档。管理操作的「立即运行」走 run。

        触发记下之后马上执行投信（run_now），通常返回时会话已经存在，调用方可以立即续写；做不到时由 worker 稍后投递。
        record_failure 为假时检查失败只上抛、不记在触发器上也不告警（见 run）。"""
        fired = await self._fire(trigger_id, payload, session_id, record_failure)
        if fired is None:
            return None
        await self._run_now(fired.delivery.id)
        return fired.session_id

    async def fire_activated(self, trigger_id: int, payload: str | None = None) -> None:
        """没有调用方等着结果的激活机制（到期扫描、流式）用的 fire：失败按统一的规则处理，不上抛领域错误。
        - 前置检查失败：已记在触发器上；脚本的故障往往是暂时的，保持启用；
        - 被停用或删除（Conflict / NotFound）：不是触发失败，只记日志，不覆盖失败原因；
        - 其余领域错误（如来信被拒绝）：停用并记 disabled_reason。
        契约之外的异常照常上抛。webhook 的调用方在等结果，直接用 fire。"""
        try:
            await self.fire(trigger_id, payload)
        except PrecheckFailed:
            pass
        except (Conflict, NotFound) as e:
            log.info("activated fire skipped: %s", e, extra={"trigger_id": trigger_id})
        except DomainError as e:
            reason = f"{type(e).__name__}: {e}"
            if await self.disable(trigger_id, reason):
                log.error("trigger disabled: %s", reason, extra={"trigger_id": trigger_id})

    async def _fire(
        self, trigger_id: int, payload: str | None, session_id: str | None, record_failure: bool
    ) -> _Fired | None:
        async with self._db.transaction() as s:
            trigger = await trigger_in(s, trigger_id)
            if not trigger.enabled:
                raise Conflict(f"trigger {trigger.name!r} is disabled")
            if trigger.precheck is None:
                return await self._deliver_in(s, trigger, payload, session_id)
            name, agent_id, stored = trigger.name, trigger.agent_id, trigger.precheck
        precheck = Precheck.model_validate(stored)

        try:
            output = await self._precheck.run(agent_id, precheck, payload)
        except PrecheckFailed as e:
            if record_failure:
                await self._record_failure(trigger_id, stored, str(e))
            raise

        failure: PrecheckFailed | None = None
        async with self._db.transaction() as s:
            trigger = await self._current(s, trigger_id, stored)
            if trigger is None:
                return None
            try:
                decision = decide(precheck, output, trigger.precheck_output, self._precheck.max_bytes)
            except PrecheckFailed as e:
                # 失败要在这个事务之外记录：异常穿出事务会把在里面写的错误与告警一起回滚
                failure = e
            else:
                self._succeed_in(trigger)
                if precheck.fire_when == "changed":
                    trigger.precheck_output = output
                if decision.event is None:
                    log.info(
                        "trigger precheck: nothing to do, not firing",
                        extra={"trigger": name, "baseline": decision.baseline},
                    )
                    return None
                return await self._deliver_in(s, trigger, decision.event, session_id)
        if record_failure:
            await self._record_failure(trigger_id, stored, str(failure))
        raise failure

    async def _current(self, s: AsyncSession, trigger_id: int, checked: dict[str, Any]) -> Trigger | None:
        """检查结果是否仍然有效：检查期间触发器被删除、停用或改了检查，这次的结果（成功或失败）都作废。
        比较的是第一次读出的原始配置，而不是重新序列化的结果。"""
        trigger = await s.get(Trigger, trigger_id, options=[undefer(Trigger.precheck_output)])
        if trigger is None or not trigger.enabled or trigger.precheck != checked:
            log.info(
                "trigger changed during its precheck, result discarded", extra={"trigger_id": trigger_id}
            )
            return None
        return trigger

    async def _record_failure(self, trigger_id: int, checked: dict[str, Any], error: str) -> None:
        async with self._db.transaction() as s:
            trigger = await self._current(s, trigger_id, checked)
            if trigger is None:
                return
            delivery = await self._fail_in(s, trigger, error)
        if delivery is not None:
            await self._run_now(delivery.id)

    def _succeed_in(self, trigger: Trigger) -> None:
        if trigger.precheck_error is not None:
            log.info(
                "trigger precheck recovered",
                extra={"trigger": trigger.name, "previous_error": trigger.precheck_error},
            )
        trigger.last_checked_at, trigger.precheck_error = self._clock(), None

    async def _fail_in(self, s: AsyncSession, trigger: Trigger, error: str) -> Task | None:
        """记下失败原因；从正常变为出错时告警一次（每分钟检查一次的触发器坏了不会每分钟告警一次），返回告警的投递任务。

        检查脚本是触发器所属的 agent 写的，告警交给它（新会话，说明出错原因和怎么处理），由它修脚本或在自己解决不了
        时通知用户；这封信投不进去（如 agent 正在删除）时退回给用户发通知，不静默吞掉。
        """
        healthy_before = trigger.precheck_error is None
        trigger.last_checked_at, trigger.precheck_error = self._clock(), error
        log.warning(
            "trigger precheck failed: %s", error, extra={"trigger": trigger.name, "alert": healthy_before}
        )
        if not healthy_before:
            return None
        now = self._clock()
        inbound = Inbound(
            origin=Origin.TRIGGER,
            origin_ref=str(trigger.id),
            content=precheck_alert(trigger, error),
            session=NewSession(
                agent_id=trigger.agent_id,
                title=f"{trigger.name} 前置检查出错 · {local_time(now, self._tz)}",
                id=new_id(),
            ),
            awaited=False,
        )
        try:
            command = await receive_command_in(s, inbound)
        except DomainError as e:
            log.warning(
                "precheck alert could not reach the agent, notifying the user: %s",
                e,
                extra={"trigger": trigger.name},
            )
            return self._messaging.send_in(
                s,
                Notify(
                    agent_id=None,
                    title=f"触发器 {trigger.name} 的前置检查出错",
                    body=error[:500],
                    session_id=None,
                    issue_id=None,
                ),
            )
        return self._messaging.send_in(s, command)


def precheck_alert(trigger: Trigger, error: str) -> str:
    """前置检查从正常变为出错时交给触发器所属 agent 的信件：出错的是哪个检查、原因，以及怎么处理。"""
    precheck = Precheck.model_validate(trigger.precheck)
    lines = [
        f"The precheck of your trigger `{trigger.name}` (kind {trigger.kind}) just started failing, so the "
        "trigger did not fire this time. You wrote this check, so it is yours to fix; the user has not been told.",
        "",
        f"fire_when: {precheck.fire_when}",
        f"secrets: {', '.join(precheck.secrets) or '(none)'}",
        "command:",
        "```bash",
        precheck.command,
        "```",
        "error:",
        "```",
        error,
        "```",
        "",
        "What to do:",
        "- A transient fault (network blip, a remote service briefly down): nothing to fix, the next check "
        "recovers by itself; consider adding retries to the command if this keeps happening.",
        "- A bug in the command: fix it with `trigger_update` (new precheck) and verify with `trigger_run`.",
        "- Something only the user can fix (a missing secret, a permission, an account): `notify_user` with what "
        "is needed.",
        "You are told only when the check goes from healthy to failing; while it keeps failing no further "
        "message comes.",
    ]
    if trigger.finished_at is not None:
        lines += [
            "",
            "This was a one-off trigger and it has used up its single firing, so its task was NOT done and it "
            "cannot be run again. Once the cause is fixed, do the task yourself or create a new trigger for it. "
            "Its prompt was:",
            "<prompt>",
            trigger.prompt,
            "</prompt>",
        ]
    return "\n".join(lines)


@dataclass(frozen=True)
class _Fired:
    session_id: str
    # 投信的定向命令（Receive）的投递任务
    delivery: Task
