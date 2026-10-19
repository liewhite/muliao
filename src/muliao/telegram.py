"""Telegram Bot API 客户端：Telegram 触发源与 Telegram 投递共用。"""

from typing import Any

import httpx

from muliao.errors import TransientError

_MAX_MESSAGE_CHARS = 4096


def split_message(text: str, limit: int = _MAX_MESSAGE_CHARS) -> list[str]:
    """按 Telegram 单条消息上限切分，尽量在换行处断开。"""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = limit if cut <= 0 else cut
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient):
        self._base = f"https://api.telegram.org/bot{token}"
        self._http = http

    async def _call(self, method: str, payload: dict[str, Any], http_timeout: float = 30.0) -> Any:
        try:
            resp = await self._http.post(f"{self._base}/{method}", json=payload, timeout=http_timeout)
        except httpx.TransportError as e:
            raise TransientError(f"telegram {method} transport error: {type(e).__name__}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"telegram {method} returned HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"telegram {method} failed: HTTP {resp.status_code} {body.get('description')}")
        return body["result"]

    async def get_updates(self, offset: int | None, poll_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": poll_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload, http_timeout=poll_seconds + 10)

    async def send_message(self, chat_id: str, text: str) -> None:
        for chunk in split_message(text):
            await self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
