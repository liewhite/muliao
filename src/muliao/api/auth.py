"""单用户认证：密码登录后下发 HMAC 签名 cookie。

签名 key 由配置的 secret_key 与密码的摘要一起派生：改了密码（改配置、重启），之前签发的 cookie 全部失效，不需要
服务端的会话表。登出只删浏览器里的 cookie。"""

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

COOKIE_NAME = "muliao_session"
MAX_AGE_SECONDS = 90 * 24 * 3600
_VERSION = "v1"


class Auth:
    def __init__(self, secret_key: str, password: str):
        self._password = password.encode()
        self._key = hmac.new(
            secret_key.encode(), hashlib.sha256(self._password).digest(), hashlib.sha256
        ).digest()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def check_password(self, password: str) -> bool:
        return hmac.compare_digest(password.encode(), self._password)

    def issue(self, now: float | None = None) -> str:
        payload = f"{_VERSION}.{int(now if now is not None else time.time())}"
        return f"{payload}.{self._sign(payload)}"

    def sign_site(self, agent_id: str, page: str) -> str:
        """agent 页面的签名 URL 段（api/sites.py）：页面在沙箱里跑、拿不到登录 cookie，凭它访问；与 cookie 同一把
        key，改了密码一并失效。"""
        return self._sign(f"site.{agent_id}/{page}")

    def verify_site(self, agent_id: str, page: str, sig: str) -> bool:
        # 按字节比：URL 里的签名可能含非 ASCII 字符，compare_digest 比 str 时会抛 TypeError
        return hmac.compare_digest(sig.encode(), self.sign_site(agent_id, page).encode())

    def verify(self, token: str, now: float | None = None) -> bool:
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _VERSION or not parts[1].isdigit():
            return False
        payload = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(parts[2], self._sign(payload)):
            return False
        issued = int(parts[1])
        return (now if now is not None else time.time()) - issued < MAX_AGE_SECONDS


class LoginThrottle:
    """登录失败按来源 IP 退避（#540）：同一 IP 连续输错，从第 FREE_FAILURES 次起每次输错后都要等一段时间才能再试，
    等待时间从 BASE_DELAY 起翻倍、最长 MAX_DELAY；等待期内连正确密码也拒绝（不校验密码），登录成功清零，
    最后一次失败过了 FORGET_AFTER 也清零。只记失败，正常输对密码的不受影响。

    来源 IP 是 request.client.host：uvicorn 的 proxy_headers 只采信受信代理（默认 127.0.0.1，本机 userspace
    tailscaled 转进来的连接就是它）带的 X-Forwarded-For，公网入口 Traefik 给的是真实 IP。tailnet 成员算可信：
    它们直连时能自填这个头。只在内存里：重启清零，可以接受。"""

    FREE_FAILURES = 5
    BASE_DELAY = 30.0
    MAX_DELAY = 15 * 60.0
    FORGET_AFTER = 3600.0

    def __init__(self) -> None:
        # ip -> (连续失败次数, 最后一次失败的时间)
        self._failures: dict[str, tuple[int, float]] = {}

    def _current(self, ip: str, now: float) -> int:
        count, last = self._failures.get(ip, (0, now))
        return count if now - last < self.FORGET_AFTER else 0

    def retry_after(self, ip: str, now: float) -> float:
        """还要等多少秒才能再试；0 表示现在可以试。"""
        count = self._current(ip, now)
        if count < self.FREE_FAILURES:
            return 0.0
        delay = min(self.BASE_DELAY * 2 ** (count - self.FREE_FAILURES), self.MAX_DELAY)
        return max(0.0, self._failures[ip][1] + delay - now)

    def failed(self, ip: str, now: float) -> None:
        count = self._current(ip, now) + 1
        # 先删再插，表按最后一次失败的时间排序（now 是单调时钟）：从表头清掉已遗忘的条目，删到第一条没过期的就停，
        # 换着 IP 试也不让表无限增长、每次失败的开销不随表长增长
        self._failures.pop(ip, None)
        while self._failures:
            oldest = next(iter(self._failures))
            if now - self._failures[oldest][1] < self.FORGET_AFTER:
                break
            del self._failures[oldest]
        self._failures[ip] = (count, now)

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)


def require_auth(request: Request) -> None:
    auth: Auth = request.app.state.auth
    token = request.cookies.get(COOKIE_NAME)
    if token is None or not auth.verify(token):
        raise HTTPException(status_code=401, detail="not authenticated")
