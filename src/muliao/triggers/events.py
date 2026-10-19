"""外部事件触发源：外部系统带 token 调用 POST /api/hooks/{name}，把事件交给指定 agent 处理一轮（新建会话或续写）。

策略：
- 不带 session_id 的事件新建一个 session，响应里返回它的 id；带上这个 id 的后续事件在同一个 session 里继续。
  触发器只能续写自己发起的 session（见 conversation.receive 的续写规则），不能写入用户或其他触发器的会话。
- token 只存 SHA-256，明文只在创建或重置时返回一次。触发器不存在与 token 不匹配返回同一个错误，不暴露哪些名称存在。
- 这一轮的回答不推送（reply_to = none），需要通知时由 agent 调用 notify_user。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound, Unauthorized
from muliao.models import Agent, EventTrigger, Origin, ReplyTo, Session
from muliao.runtime.conversation import Inbound, NewSession, receive
from muliao.runtime.tasks import Clock, TaskQueue

log = logging.getLogger(__name__)

# 事件内容原样进入 prompt，过大的事件应由发送方裁剪或改为传引用
EVENT_MAX_BYTES = 256 * 1024


# 出现在 URL 里；不含下划线，使名称到 vault key（见 tools/triggers.token_key）一一对应
_TRIGGER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def validate_trigger_name(name: str) -> str:
    if not _TRIGGER_NAME.fullmatch(name):
        raise InvalidInput(f"invalid event trigger name {name!r}: must match {_TRIGGER_NAME.pattern}")
    return name


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hook_url(public_url: str, name: str) -> str:
    return f"{public_url.rstrip('/')}/api/hooks/{name}"


def event_prompt(instructions: str, payload: str) -> str:
    return f"{instructions.strip()}\n\n<event>\n{payload}\n</event>"


class EventTriggers:
    def __init__(self, db: Database, queue: TaskQueue, tz: ZoneInfo, clock: Clock = utcnow):
        self._db = db
        self._queue = queue
        self._tz = tz
        self._clock = clock

    async def create(self, *, name: str, agent_id: str, prompt: str) -> tuple[EventTrigger, str]:
        """返回触发器与 token 明文（只此一次）。"""
        if not prompt.strip():
            raise InvalidInput("event trigger prompt must not be empty")
        token = secrets.token_urlsafe(32)
        trigger = EventTrigger(
            name=validate_trigger_name(name),
            agent_id=agent_id,
            prompt=prompt,
            token_hash=_hash(token),
            enabled=True,
        )
        try:
            async with self._db.transaction() as s:
                if await s.get(Agent, agent_id) is None:
                    raise NotFound(f"agent {agent_id!r} not found")
                s.add(trigger)
        except IntegrityError as e:
            raise Conflict(f"event trigger {name!r} already exists") from e
        log.info("event trigger created", extra={"trigger": name, "agent": agent_id})
        return trigger, token

    async def update(
        self, trigger_id: int, *, prompt: str | None = None, enabled: bool | None = None
    ) -> EventTrigger:
        """在一个事务里只修改给出的字段（None 不变），并发的局部修改互不覆盖。"""
        if prompt is not None and not prompt.strip():
            raise InvalidInput("event trigger prompt must not be empty")
        async with self._db.transaction() as s:
            trigger = await self._get(s, trigger_id)
            if prompt is not None:
                trigger.prompt = prompt
            if enabled is not None:
                trigger.enabled = enabled
        log.info("event trigger updated", extra={"trigger": trigger.name, "enabled": trigger.enabled})
        return trigger

    async def rotate_token(self, trigger_id: int) -> str:
        token = secrets.token_urlsafe(32)
        async with self._db.transaction() as s:
            trigger = await self._get(s, trigger_id)
            trigger.token_hash = _hash(token)
        log.info("event trigger token rotated", extra={"trigger_id": trigger_id})
        return token

    async def delete(self, trigger_id: int) -> None:
        async with self._db.transaction() as s:
            trigger = await self._get(s, trigger_id)
            await s.delete(trigger)
        log.info("event trigger deleted", extra={"trigger": trigger.name, "trigger_id": trigger_id})

    async def list(self, agent_id: str | None = None) -> list[EventTrigger]:
        query = select(EventTrigger).order_by(EventTrigger.name)
        if agent_id is not None:
            query = query.where(EventTrigger.agent_id == agent_id)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def get_by_name(self, name: str) -> EventTrigger:
        async with self._db.transaction() as s:
            trigger = await s.scalar(select(EventTrigger).where(EventTrigger.name == name))
            if trigger is None:
                raise NotFound(f"event trigger {name!r} not found")
            return trigger

    async def _get(self, s: AsyncSession, trigger_id: int) -> EventTrigger:
        trigger = await s.get(EventTrigger, trigger_id)
        if trigger is None:
            raise NotFound(f"event trigger {trigger_id} not found")
        return trigger

    async def authenticate(self, name: str, token: str) -> int:
        """校验 token，返回触发器 id。触发器不存在与 token 不匹配返回同一个错误，且都要计算一次摘要。"""
        digest = _hash(token)
        async with self._db.transaction() as s:
            trigger = await s.scalar(select(EventTrigger).where(EventTrigger.name == name))
            if trigger is None or not hmac.compare_digest(trigger.token_hash, digest):
                log.warning("event rejected: unknown trigger or wrong token", extra={"trigger": name})
                raise Unauthorized("unknown event trigger or wrong token")
            return trigger.id

    async def deliver(self, trigger_id: int, payload: str, session_id: str | None) -> Session:
        """把已鉴权的事件交给 agent：session_id 为空时新建会话，否则在该会话里继续。"""
        if not payload.strip():
            raise InvalidInput("event payload must not be empty")
        async with self._db.transaction() as s:
            # 鉴权之后、投递之前触发器可能被删除或停用
            trigger = await self._get(s, trigger_id)
            if not trigger.enabled:
                raise Conflict(f"event trigger {trigger.name!r} is disabled")
            now = self._clock()
            new = NewSession(agent_id=trigger.agent_id, title=f"{trigger.name} · {_local(now, self._tz)}")
            received = await receive(
                s,
                self._queue,
                Inbound(
                    source=Origin.EVENT,
                    source_ref=str(trigger.id),
                    content=event_prompt(trigger.prompt, payload),
                    session=new if session_id is None else session_id,
                    reply_to=ReplyTo.NONE,
                ),
            )
            session = received.session
            trigger.last_fired_at = now
            trigger.last_session_id = session.id
            name, agent_id = trigger.name, trigger.agent_id
        log.info("event delivered", extra={"trigger": name, "agent": agent_id, "session_id": session.id})
        return session


def _local(now: datetime, tz: ZoneInfo) -> str:
    return now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
