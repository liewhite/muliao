"""触发器：具名、持久的事件来源，把外部刺激变成交给某个 agent 的一轮（见 models.Trigger）。

触发方式是多态的，分两部分实现，新增一种方式（例如消息队列）只需各加一个实现并在组合根注册：
- TriggerKind：纯逻辑——校验配置、推导运行状态，注册给 TriggerService；
- 激活机制：怎么被触发——定时循环（schedule.ScheduleRunner）、HTTP 入口（webhook.Webhooks）、消费循环……
  它们都只调用 TriggerService.fire，组装成事件交给会话领域的统一入口。

所有触发产生的一轮都不推送（reply_to = none），需要通知时由 agent 调用 notify_user。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.models import Agent, Origin, ReplyTo, Session, Trigger
from muliao.runtime.conversation import Inbound, NewSession, receive
from muliao.runtime.tasks import Clock, TaskQueue, turn_lane

log = logging.getLogger(__name__)

# 出现在 webhook 地址里
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


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

    def status(self, trigger: Trigger) -> dict[str, Any]:
        """给界面与模型看的运行状态（如下次触发时间、地址），不含任何凭证。"""
        ...


def validate_trigger_name(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise InvalidInput(f"invalid trigger name {name!r}: must match {_NAME.pattern}")
    return name


def event_prompt(instructions: str, payload: str | None) -> str:
    if payload is None:
        return instructions
    return f"{instructions.strip()}\n\n<event>\n{payload}\n</event>"


class TriggerService:
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        kinds: Mapping[str, TriggerKind],
        tz: ZoneInfo,
        clock: Clock = utcnow,
    ):
        self._db = db
        self._queue = queue
        self.kinds = dict(kinds)
        self._tz = tz
        self._clock = clock
        # 每个激活机制各自订阅变化通知（见 subscribe），互不清除对方的通知
        self._subscribers: list[asyncio.Event] = []

    def subscribe(self) -> asyncio.Event:
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

    async def create(
        self, *, name: str, agent_id: str, kind: str, prompt: str, config: dict[str, Any]
    ) -> Trigger:
        if not prompt.strip():
            raise InvalidInput("trigger prompt must not be empty")
        config, state = self.kind(kind).configure(config)
        trigger = Trigger(
            name=validate_trigger_name(name),
            agent_id=agent_id,
            prompt=prompt,
            kind=kind,
            config=config,
            state=state,
            enabled=True,
        )
        try:
            async with self._db.transaction() as s:
                if await s.get(Agent, agent_id) is None:
                    raise NotFound(f"agent {agent_id!r} not found")
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
    ) -> Trigger:
        """在一个事务里只修改给出的字段（None 不变）。重新启用时清除上次的失败原因。"""
        if prompt is not None and not prompt.strip():
            raise InvalidInput("trigger prompt must not be empty")
        async with self._db.transaction() as s:
            trigger = await self.get_in(s, trigger_id)
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
                if enabled:
                    trigger.last_error = None
        self._changed()
        log.info("trigger updated", extra={"trigger": trigger.name, "enabled": trigger.enabled})
        return trigger

    @staticmethod
    def set_state(trigger: Trigger, **values: Any) -> None:
        """激活机制维护自己的运行状态（在调用方事务里改这一行）。JSON 列只在整体赋值时被追踪。"""
        trigger.state = {**trigger.state, **values}

    async def delete(self, trigger_id: int) -> None:
        async with self._db.transaction() as s:
            trigger = await self.get_in(s, trigger_id)
            await s.delete(trigger)
        self._changed()
        log.info("trigger deleted", extra={"trigger": trigger.name, "trigger_id": trigger_id})

    async def list(self, *, kind: str | None = None, enabled_only: bool = False) -> list[Trigger]:
        query = select(Trigger).order_by(Trigger.name)
        if kind is not None:
            query = query.where(Trigger.kind == kind)
        if enabled_only:
            query = query.where(Trigger.enabled)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def get_in(self, s: AsyncSession, trigger_id: int) -> Trigger:
        trigger = await s.get(Trigger, trigger_id)
        if trigger is None:
            raise NotFound(f"trigger {trigger_id} not found")
        return trigger

    async def get_by_name(self, name: str) -> Trigger:
        async with self._db.transaction() as s:
            trigger = await s.scalar(select(Trigger).where(Trigger.name == name))
            if trigger is None:
                raise NotFound(f"trigger {name!r} not found")
            return trigger

    async def busy_in(self, s: AsyncSession, trigger: Trigger) -> bool:
        """上一次触发的会话是否还有未完成的一轮（定时触发据此避免重叠）。"""
        return trigger.last_session_id is not None and await self._queue.lane_busy(
            s, turn_lane(trigger.last_session_id)
        )

    async def fire_in(
        self, s: AsyncSession, trigger: Trigger, payload: str | None, session_id: str | None
    ) -> Session:
        """所有触发方式的唯一出口：组装成事件交给会话领域。

        payload 为事件内容（无则只有处理说明）；session_id 为空时新建会话，否则续写——只能续写本触发器发起的会话。
        """
        if not trigger.enabled:
            raise Conflict(f"trigger {trigger.name!r} is disabled")
        now = self._clock()
        received = await receive(
            s,
            self._queue,
            Inbound(
                origin=Origin.TRIGGER,
                origin_ref=str(trigger.id),
                content=event_prompt(trigger.prompt, payload),
                session=NewSession(
                    agent_id=trigger.agent_id, title=f"{trigger.name} · {_local(now, self._tz)}"
                )
                if session_id is None
                else session_id,
                reply_to=ReplyTo.NONE,
            ),
        )
        trigger.last_fired_at = now
        trigger.last_session_id = received.session.id
        log.info(
            "trigger fired",
            extra={"trigger": trigger.name, "kind": trigger.kind, "session_id": received.session.id},
        )
        return received.session

    async def fire(
        self, trigger_id: int, payload: str | None = None, session_id: str | None = None
    ) -> Session:
        async with self._db.transaction() as s:
            return await self.fire_in(s, await self.get_in(s, trigger_id), payload, session_id)


def _local(now: datetime, tz: ZoneInfo) -> str:
    return now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
