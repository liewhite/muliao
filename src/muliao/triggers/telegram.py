"""Telegram 触发源：长轮询 bot 消息，每个 chat 复用最近的 session，`/new` 新开。

至少一次语义：getUpdates 的 offset 只在内存中推进，进程重启时最后一批未确认的消息可能重复处理。
"""

import asyncio
import logging
from typing import Any

from muliao.delivery.base import Notification, Notifier
from muliao.errors import TransientError
from muliao.models import DeliveryChannel, Origin
from muliao.runtime.sessions import SessionService
from muliao.telegram import TelegramClient

log = logging.getLogger(__name__)

_POLL_TIMEOUT = 50
_ERROR_BACKOFF_SECONDS = 10.0
NEW_SESSION_COMMAND = "/new"


class TelegramTrigger:
    """只负责把消息转成 task；回复与提示都经 deliver task 发出，享受统一重试，轮询不会被网络错误打断。"""

    def __init__(
        self,
        client: TelegramClient,
        sessions: SessionService,
        notifier: Notifier,
        agent_name: str,
        allowed_chat_ids: set[int],
    ):
        self._client = client
        self._sessions = sessions
        self._notifier = notifier
        self._agent = agent_name
        self._allowed = allowed_chat_ids

    async def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if message is None:
            log.debug("telegram update without message ignored", extra={"update_id": update["update_id"]})
            return
        chat_id = message["chat"]["id"]
        if chat_id not in self._allowed:
            log.warning("telegram message from non-allowed chat dropped", extra={"chat_id": chat_id})
            return
        ref = str(chat_id)
        text = message.get("text")
        if not text:
            await self._tell(ref, "Only text messages are supported.")
            return
        if text.strip() == NEW_SESSION_COMMAND:
            await self._sessions.start(
                agent_name=self._agent,
                prompt="(The user started a new conversation. Greet them briefly.)",
                origin=Origin.TELEGRAM,
                origin_ref=ref,
                delivery=DeliveryChannel.TELEGRAM,
                delivery_target=ref,
                title=f"Telegram {ref}",
            )
            return
        session = await self._sessions.find_latest(Origin.TELEGRAM, ref)
        if session is None:
            await self._sessions.start(
                agent_name=self._agent,
                prompt=text,
                origin=Origin.TELEGRAM,
                origin_ref=ref,
                delivery=DeliveryChannel.TELEGRAM,
                delivery_target=ref,
            )
        else:
            await self._sessions.reply(session.id, text)

    async def _tell(self, chat_id: str, text: str) -> None:
        await self._notifier.notify(
            self._agent,
            Notification(
                channel=DeliveryChannel.TELEGRAM, target=chat_id, title="", body=text, session_id=None
            ),
        )

    async def run_forever(self) -> None:
        offset: int | None = None
        log.info("telegram polling started", extra={"agent": self._agent})
        while True:
            try:
                updates = await self._client.get_updates(offset, _POLL_TIMEOUT)
            except TransientError as e:
                log.warning("telegram polling failed transiently, backing off: %s", e)
                await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
                continue
            for update in updates:
                await self.handle(update)
                offset = update["update_id"] + 1
