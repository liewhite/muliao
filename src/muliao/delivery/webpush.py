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
from muliao.errors import InvalidInput, TransientError
from muliao.models import Notification, PushSubscription
from muliao.transcript import truncate

log = logging.getLogger(__name__)

# RFC 8030：404/410 表示订阅已失效，应删除
_GONE = {404, 410}
# 网络层失败（无 HTTP 响应）用 0 表示
_NO_RESPONSE = 0
_TRANSIENT = {_NO_RESPONSE, 408, 429, 500, 502, 503, 504}
# 推送服务限制载荷约 4KB（加密后），正文截断留足余量
_BODY_MAX_CHARS = 600
_TTL_SECONDS = 24 * 3600


class VapidKeys:
    def __init__(self, key_path: Path):
        if not key_path.exists():
            key = Vapid02()
            key.generate_keys()
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key.save_key(str(key_path))
            key_path.chmod(0o600)
            log.info("generated VAPID key", extra={"path": str(key_path)})
        if key_path.stat().st_mode & 0o077:
            # 私钥只应属主可读
            log.warning(
                "VAPID private key was readable by others, restricting to 0600", extra={"path": str(key_path)}
            )
            key_path.chmod(0o600)
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

    async def send(self, notification: Notification, unread: int) -> None:
        """逐个订阅独立投递：失效订阅删除，暂时性失败汇总后抛出交给任务重试，载荷错误属于本端 bug。"""
        subs = await self._subs.list()
        if not subs:
            log.warning(
                "no push subscriptions, notification dropped", extra={"session_id": notification.session_id}
            )
            return
        data = json.dumps(
            {
                "title": notification.title,
                "body": truncate(notification.body, _BODY_MAX_CHARS),
                # 系统通知不指向会话，点开看通知页
                "url": f"/sessions/{notification.session_id}"
                if notification.session_id
                else "/notifications",
                "session_id": notification.session_id,
                "badge": unread,
            },
            ensure_ascii=False,
        )
        transient: list[str] = []
        rejected: list[int] = []
        sent = gone = 0
        for sub in subs:
            status = await asyncio.to_thread(self._push, sub, data)
            if status is None:
                sent += 1
                continue
            if status in _GONE:
                log.info("push subscription gone, removing it", extra={"status": status})
                await self._subs.remove(sub.endpoint)
                gone += 1
            elif status in _TRANSIENT:
                transient.append(str(status))
            else:
                # 其余 4xx（400/401/403/413…）多半是本端问题（VAPID 声明、时钟、载荷），不能删用户的订阅
                rejected.append(status)
        log.info(
            "web push sent",
            extra={
                "notification_id": notification.id,
                "session_id": notification.session_id,
                "sent": sent,
                "gone": gone,
                "failed": len(transient) + len(rejected),
            },
        )
        if rejected:
            raise RuntimeError(f"push service rejected {len(rejected)} subscriptions with HTTP {rejected}")
        if transient:
            raise TransientError(
                f"push service temporarily failed for {len(transient)} subscriptions: {transient}"
            )

    def _push(self, sub: PushSubscription, data: str) -> int | None:
        """返回失败的 HTTP 状态码；成功返回 None。"""
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
            # 没有响应即网络层失败
            return e.response.status_code if e.response is not None else _NO_RESPONSE
        except (requests.ConnectionError, requests.Timeout):
            return _NO_RESPONSE
        return None
