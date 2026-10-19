from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import requests
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02, b64urlencode
from pywebpush import WebPushException, webpush
from sqlalchemy import select

from muliao.db import Database
from muliao.delivery.base import Notification
from muliao.errors import InvalidInput, TransientError
from muliao.models import PushSubscription

log = logging.getLogger(__name__)

# RFC 8030：404/410 表示订阅已失效，应删除
_GONE = {404, 410}
_TRANSIENT = {408, 429, 500, 502, 503, 504}
_TTL_SECONDS = 24 * 3600


class VapidKeys:
    def __init__(self, key_path: Path):
        if not key_path.exists():
            key = Vapid02()
            key.generate_keys()
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key.save_key(str(key_path))
            log.info("generated VAPID key", extra={"path": str(key_path)})
        self.vapid = Vapid02.from_file(str(key_path))
        raw = self.vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        # 浏览器 PushManager.subscribe 的 applicationServerKey
        self.public_key_b64 = b64urlencode(raw)


class PushSubscriptions:
    def __init__(self, db: Database):
        self._db = db

    async def add(self, endpoint: str, p256dh: str, auth: str) -> None:
        if not endpoint.startswith("https://"):
            raise InvalidInput("push endpoint must be an https URL")
        async with self._db.transaction() as s:
            row = await s.get(PushSubscription, endpoint)
            if row is None:
                s.add(PushSubscription(endpoint=endpoint, p256dh=p256dh, auth=auth))
            else:
                row.p256dh, row.auth = p256dh, auth

    async def remove(self, endpoint: str) -> None:
        async with self._db.transaction() as s:
            row = await s.get(PushSubscription, endpoint)
            if row is not None:
                await s.delete(row)

    async def list(self) -> list[PushSubscription]:
        async with self._db.transaction() as s:
            return list((await s.scalars(select(PushSubscription))).all())


class WebPushDelivery:
    def __init__(self, subscriptions: PushSubscriptions, keys: VapidKeys, subject: str):
        self._subs = subscriptions
        self._keys = keys
        self._subject = subject

    async def send(self, notification: Notification) -> None:
        subs = await self._subs.list()
        if not subs:
            log.warning(
                "no push subscriptions, notification dropped", extra={"session_id": notification.session_id}
            )
            return
        data = json.dumps(
            {
                "title": notification.title,
                "body": notification.body,
                "url": f"/sessions/{notification.session_id}" if notification.session_id else "/",
                "session_id": notification.session_id,
            }
        )
        transient: list[str] = []
        for sub in subs:
            status = await asyncio.to_thread(self._push, sub, data)
            if status is None:
                continue
            if status in _GONE:
                log.info("push subscription gone, removing it", extra={"status": status})
                await self._subs.remove(sub.endpoint)
            elif status in _TRANSIENT:
                transient.append(str(status))
            else:
                raise RuntimeError(f"push service rejected notification with HTTP {status}")
        if transient:
            raise TransientError(
                f"push service temporarily failed for {len(transient)} subscriptions: {transient}"
            )

    def _push(self, sub: PushSubscription, data: str) -> int | None:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=data,
                vapid_private_key=self._keys.vapid,
                vapid_claims={"sub": self._subject},
                ttl=_TTL_SECONDS,
                timeout=15,
            )
        except WebPushException as e:
            if e.response is None:
                raise TransientError(f"push request failed without response: {e}") from e
            return e.response.status_code
        except requests.RequestException as e:
            raise TransientError(f"push request failed: {type(e).__name__}") from e
        return None
