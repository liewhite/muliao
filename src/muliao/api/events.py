"""推给前端的服务端事件（SSE）：只用于展示加速，数据库是唯一事实源，收到状态变化时前端从接口重新拉取。

形状在这里声明一次：SSE 只序列化这些模型，它们也登记进 OpenAPI（api.app），前端类型由此生成。
"""

from typing import Literal

from pydantic import BaseModel, RootModel

from muliao.notifications.events import NotificationEvent
from muliao.platform.bus import Event, TaskEvent
from muliao.platform.tables import TaskStatus
from muliao.schema import ChangedEvent, Resource
from muliao.sessions.api import RUN_TASK, RunDeltaEvent, RunItemEvent, SavedEvent


class HeartbeatEvent(BaseModel):
    """连上就发一次、之后每隔一段时间发一次：前端据此判断连接是否还活着（间隔随心跳下发），并比较前端构建的版本。"""

    type: Literal["heartbeat"] = "heartbeat"
    interval_seconds: float
    # 服务端当前前端构建的版本；包里没有前端构建时为 None
    ui_version: str | None


class ServerEvent(
    RootModel[
        TaskEvent
        | SavedEvent
        | RunItemEvent
        | RunDeltaEvent
        | NotificationEvent
        | ChangedEvent
        | HeartbeatEvent
    ]
):
    """SSE 里每条 data 的形状（api.app 的 /events）。具名是为了在 OpenAPI 里成为一个组件：前端的事件联合直接取它，
    不另列一遍（openapi-typescript 还不认识 OpenAPI 3.2 的 itemSchema）。OpenAPI 的 discriminator 不能嵌套，这里不标。"""


def derived_changes(event: Event) -> list[ChangedEvent]:
    """接口输出里有由别的数据派生的部分：源头变了，展示它的类别也算变了（SSE 接口紧跟在 event 后面发）。前端只认 changed，
    不自己推算哪些页面受影响。

    - 任务状态（任何种类）：任务列表；会话与 issue 上的工作状态（sessions.activity，看会话与收信 lane 上的任务）。
    - 一次运行结束：agent 在运行里直接改 AI 资源文件（文件工具、shell），服务端看不到具体哪次写，结束时以 agents 发。
    - 会话：issue 上的工作状态（占用、挂起的提问也算）与各 agent 的 issue 会话由会话领域派生（issues.service.IssueWork）。
    - 通知：会话列表的未读由通知派生（api.sessions）。"""
    resources: list[Resource] = []
    if isinstance(event, TaskEvent):
        resources += ["tasks", "sessions", "issues"]
        if event.kind == RUN_TASK and event.status in (TaskStatus.DONE, TaskStatus.FAILED):
            resources.append("agents")
    elif isinstance(event, ChangedEvent) and event.resource == "sessions":
        resources.append("issues")
    elif isinstance(event, ChangedEvent) and event.resource == "notifications":
        resources.append("sessions")
    return [ChangedEvent(resource=r) for r in resources]
