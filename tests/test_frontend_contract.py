"""前后端各写一份的常量与路由：前端（web/src）的值必须与后端的唯一来源一致。接口里的类型由 OpenAPI 生成，这里管的是
不在接口类型里的那几个值（指代用户的值、系统 actor、文件地址前缀、通知点开的页面路由）。"""

import re
from pathlib import Path

from muliao.agents.api import USER
from muliao.files.api import FILES_PATH
from muliao.issues.rules import SYSTEM_ACTOR
from muliao.notifications.tables import Notification

WEB = Path(__file__).parent.parent / "web" / "src"


def _const(file: str, name: str) -> str:
    match = re.search(rf"^export const {name} = '([^']*)'$", (WEB / file).read_text(), re.MULTILINE)
    assert match is not None, f"{name} not found in web/src/{file}"
    return match[1]


def _route(file: str, name: str, arg: str) -> str:
    """前端路由函数 `(id: ...) => `...${id}`` 代入 arg。"""
    match = re.search(
        rf"^export const {name} = \(id: \w+\) => `([^`]*)`$", (WEB / file).read_text(), re.MULTILINE
    )
    assert match is not None, f"{name} not found in web/src/{file}"
    return match[1].replace("${id}", arg)


def test_frontend_constants_match_the_backend():
    assert _const("api.ts", "USER") == USER
    assert _const("api.ts", "SYSTEM_ACTOR") == SYSTEM_ACTOR
    assert _const("api.ts", "FILES_PATH") == FILES_PATH


def test_notification_urls_are_the_frontend_routes():
    """通知点开的地址（推送与通知页共用，Notification.url）就是前端的会话页、issue 页路由。"""
    assert Notification(session_id="s1").url == _route("api.ts", "sessionRoute", "s1")
    assert Notification(session_id="s1", issue_id=7).url == _route("issues.ts", "issueRoute", "7")
