"""推给前端的服务端事件（SSE）：只用于展示加速，数据库是唯一事实源，收到状态变化时前端从接口重新拉取。

形状在这里声明一次：SSE 只序列化这些模型，它们也登记进 OpenAPI（api.app），前端类型由此生成。
"""

from typing import Literal

from pydantic import BaseModel, RootModel

from muliao.platform.bus import TaskEvent
from muliao.sessions.live import RunDeltaEvent, RunEvent, RunItemEvent, SavedEvent

__all__ = ["HeartbeatEvent", "RunDeltaEvent", "RunEvent", "RunItemEvent", "SavedEvent", "ServerEvent", "TaskEvent"]


class HeartbeatEvent(BaseModel):
    """连上就发一次、之后每隔一段时间发一次：前端据此判断连接是否还活着（间隔随心跳下发），并比较前端构建的版本。"""

    type: Literal["heartbeat"] = "heartbeat"
    interval_seconds: float
    # 服务端当前前端构建的版本；包里没有前端构建时为 None
    ui_version: str | None


class ServerEvent(RootModel[TaskEvent | SavedEvent | RunItemEvent | RunDeltaEvent | HeartbeatEvent]):
    """SSE 里每条 data 的形状（api.app 的 /events）。具名是为了在 OpenAPI 里成为一个组件：前端的事件联合直接取它，
    不另列一遍（openapi-typescript 还不认识 OpenAPI 3.2 的 itemSchema）。OpenAPI 的 discriminator 不能嵌套，这里不标。"""
