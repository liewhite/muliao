import logging

from muliao.delivery.base import Notification
from muliao.telegram import TelegramClient

log = logging.getLogger(__name__)


class TelegramDelivery:
    """把 session 结果发回发起它的 Telegram chat。"""

    def __init__(self, client: TelegramClient):
        self._client = client

    async def send(self, notification: Notification) -> None:
        if notification.target is None:
            raise ValueError("telegram notification requires a target chat id")
        await self._client.send_message(notification.target, notification.body)
        log.info(
            "telegram message sent",
            extra={"chat_id": notification.target, "session_id": notification.session_id},
        )
