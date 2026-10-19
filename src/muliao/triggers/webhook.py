"""webhook 触发源：外部系统 POST 事件，每个事件为指定 agent 新建 session。"""

from __future__ import annotations

import hmac
import logging
import secrets

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from muliao.db import Database
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.ids import new_id
from muliao.models import Agent, DeliveryChannel, Origin, Session, Webhook
from muliao.runtime.sessions import SessionService

log = logging.getLogger(__name__)

MAX_EVENT_BYTES = 100_000


def event_prompt(instructions: str, body: str) -> str:
    return f"{instructions.strip()}\n\n<event>\n{body}\n</event>"


class WebhookService:
    def __init__(self, db: Database, sessions: SessionService):
        self._db = db
        self._sessions = sessions

    async def create(self, *, name: str, agent_name: str, prompt: str) -> Webhook:
        if not name.strip() or not prompt.strip():
            raise InvalidInput("webhook name and prompt must not be empty")
        hook = Webhook(
            id=new_id(),
            name=name.strip(),
            agent_name=agent_name,
            token=secrets.token_urlsafe(32),
            prompt=prompt,
        )
        try:
            async with self._db.transaction() as s:
                if await s.get(Agent, agent_name) is None:
                    raise NotFound(f"agent {agent_name!r} not found")
                s.add(hook)
        except IntegrityError as e:
            raise Conflict(f"webhook {name!r} already exists") from e
        return hook

    async def list(self) -> list[Webhook]:
        async with self._db.transaction() as s:
            return list((await s.scalars(select(Webhook).order_by(Webhook.name))).all())

    async def delete(self, hook_id: str) -> None:
        async with self._db.transaction() as s:
            hook = await s.get(Webhook, hook_id)
            if hook is None:
                raise NotFound(f"webhook {hook_id} not found")
            await s.delete(hook)

    async def receive(self, hook_id: str, token: str, body: str) -> Session:
        if len(body.encode()) > MAX_EVENT_BYTES:
            raise InvalidInput(f"event body exceeds {MAX_EVENT_BYTES} bytes")
        async with self._db.transaction() as s:
            hook = await s.get(Webhook, hook_id)
            # 未知 id 与错误 token 返回同一错误，避免探测
            if hook is None or not hmac.compare_digest(hook.token, token):
                log.warning("webhook rejected: unknown id or bad token", extra={"webhook_id": hook_id})
                raise Forbidden("invalid webhook id or token")
            session = await self._sessions.create_in(
                s,
                agent_name=hook.agent_name,
                prompt=event_prompt(hook.prompt, body),
                origin=Origin.WEBHOOK,
                origin_ref=hook.id,
                delivery=DeliveryChannel.WEBPUSH,
                title=f"{hook.name} event",
            )
        log.info("webhook event accepted", extra={"webhook": hook.name, "session_id": session.id})
        return session
