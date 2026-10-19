"""单用户认证：密码登录后下发 HMAC 签名 cookie。"""

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

COOKIE_NAME = "muliao_session"
MAX_AGE_SECONDS = 90 * 24 * 3600
_VERSION = "v1"


class Auth:
    def __init__(self, secret_key: str, password: str):
        self._key = secret_key.encode()
        self._password = password.encode()

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def check_password(self, password: str) -> bool:
        return hmac.compare_digest(password.encode(), self._password)

    def issue(self, now: float | None = None) -> str:
        payload = f"{_VERSION}.{int(now if now is not None else time.time())}"
        return f"{payload}.{self._sign(payload)}"

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
