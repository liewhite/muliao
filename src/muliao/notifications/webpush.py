from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import requests
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from py_vapid import Vapid02, b64urlencode
from pywebpush import WebPushException, webpush

from muliao.errors import InvalidInput, ServiceUnavailable
from muliao.notifications.tables import Notification, PushSubscription
from muliao.platform.db import Database
from muliao.platform.text_edit import truncate

log = logging.getLogger(__name__)

# RFC 8030：404/410 表示订阅已失效，应删除
_GONE = {404, 410}
# 网络层失败（无 HTTP 响应）用 0 表示
_NO_RESPONSE = 0
_TRANSIENT = {_NO_RESPONSE, 408, 429, 500, 502, 503, 504}
_TRANSPORT_FAILURES = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
# 推送服务限制载荷约 4KB（加密后），正文截断留足余量
_BODY_MAX_CHARS = 600
# 同时进行的推送请求（pywebpush 是同步的，每个占一个线程，最长约 30 秒）：任务不设并发上限（platform.worker），
# 推送服务变慢时不能占满线程池、拖慢别的线程上的工作（附件读取、文件写入），在用的地方限流
# 被取消（关停）时等线程里的请求结束（最长约 30 秒）再退出，不在后台留下无人管的线程；没做完的投递由启动恢复重做
_PUSH_THREADS = 4
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


class WebPushDelivery:
    def __init__(self, subscriptions: PushSubscriptions, keys: VapidKeys, subject: str):
        self._subs = subscriptions
        self._keys = keys
        self._subject = subject
        self._threads = anyio.CapacityLimiter(_PUSH_THREADS)

    async def send(self, subscription: PushSubscription, notification: Notification, unread: int) -> None:
        """投递到一个订阅：失效订阅删除，暂时性失败抛 ServiceUnavailable 交给任务按长窗口（约 30 分钟）重试（只重推
        这一台：本机到推送服务的网络断开常超过十分钟，#208），其余 4xx 属于本端问题。"""
        data = json.dumps(
            {
                "title": notification.title,
                "body": truncate(notification.body, _BODY_MAX_CHARS),
                # 系统通知不指向别处，点开看通知页
                "url": notification.url or "/notifications",
                "session_id": notification.session_id,
                # 有它时 Service Worker 给通知加「通过」「打回」按钮（只有 Chromium 显示）
                "issue_id": notification.issue_id,
                "badge": unread,
            },
            ensure_ascii=False,
        )
        status = await anyio.to_thread.run_sync(self._push, subscription, data, limiter=self._threads)
        # 不记 endpoint：它的完整路径即订阅凭据，只记推送服务的主机
        host = urlsplit(subscription.endpoint).hostname
        extra = {
            "notification_id": notification.id,
            "session_id": notification.session_id,
            "push_host": host,
            "status": status,
        }
        if status is None:
            log.info("web push sent", extra=extra)
        elif status in _GONE:
            log.info("push subscription gone, removing it", extra=extra)
            await self._subs.remove(subscription.endpoint)
        elif status in _TRANSIENT:
            raise ServiceUnavailable(f"push service temporarily failed with HTTP {status} ({host})")
        else:
            # 其余 4xx（400/401/403/413…）多半是本端问题（VAPID 声明、时钟、载荷），不能删用户的订阅；task 失败可见、不重试
            raise RuntimeError(f"push service rejected the subscription with HTTP {status} ({host})")

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
        except _TRANSPORT_FAILURES as e:
            # 没有拿到推送服务的响应（连接、超时、读到一半断开）：按暂时性失败处理，不中断循环、不让剩下的设备收不到。
            # 其余 requests 异常（URL、请求头不合法……）是本端问题，重试也不会好，照常抛出
            log.warning(
                "push transport failed: %s",
                type(e).__name__,
                # 不记异常文本：它带着 endpoint 的完整路径，即订阅凭据
                extra={"push_host": urlsplit(sub.endpoint).hostname},
            )
            return _NO_RESPONSE
        except requests.RequestException:
            # requests 的异常都是 OSError 的子类：URL、请求头不合法这类本端问题在这里照常抛出，不落进下面的 OSError
            raise
        except OSError as e:
            # 本机文件或环境失败，多半在请求发出前（如部署时 uv tool install --force 原地替换安装目录，cert_verify
            # 找不到 certifi 的 CA bundle，#188）：按暂时性失败重试，服务重启后由新进程接着投；万一已经发出，最多对
            # 这一台重推一次（与崩溃后重推同一代价）。连接、收发的错误都被 requests 包成 RequestException，落不到这里，
            # 所以这里的异常文本只含本机文件路径、没有 endpoint，可以记
            log.warning(
                "push failed locally before sending: %s",
                e,
                extra={"push_host": urlsplit(sub.endpoint).hostname},
            )
            return _NO_RESPONSE
        return None
