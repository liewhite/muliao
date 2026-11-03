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


def require_auth(request: Request) -> None:
    auth: Auth = request.app.state.auth
    token = request.cookies.get(COOKIE_NAME)
    if token is None or not auth.verify(token):
        raise HTTPException(status_code=401, detail="not authenticated")
