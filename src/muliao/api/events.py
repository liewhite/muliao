"""推给前端的服务端事件（SSE）：只用于展示加速，数据库是唯一事实源，收到状态变化时前端从接口重新拉取。

形状在这里声明一次：SSE 只序列化这些模型，它们也登记进 OpenAPI（api.app），前端类型由此生成。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, RootModel

from muliao.models import TaskKind, TaskStatus
from muliao.sessions.transcript import Item


class TaskEvent(BaseModel):
    """一个 task 的状态变化（前端据此从数据库重新拉取），由 worker 发出。"""

    type: Literal["task"] = "task"
    task_id: int
    kind: TaskKind
    status: TaskStatus
    session_id: str | None
    # 系统通知的投递没有所属 agent
    agent: str | None
    error: str | None


class SavedEvent(BaseModel):
    """运行中又保存了一步（bus.LiveRuns.step）：历史有了新内容，实时事件换成新的一段。"""

    type: Literal["saved"] = "saved"
    session_id: str


class _RunEventBase(BaseModel):
    type: Literal["run"] = "run"
    session_id: str
    agent: str
    # 这一段的编号与事件序号（bus.LiveRuns）：快照与 SSE 事件据此合并、去重、发现缺口
    run_id: str
    seq: int


class RunItemEvent(_RunEventBase):
    """一条完整的会话条目（工具调用、工具结果），与历史同一个投影（transcript）；文字可能截断过。"""

    event: Literal["item"] = "item"
    item: Item


class RunDeltaEvent(_RunEventBase):
    """流式文字：接在同类的最后一条后面（或开始新的一条）。"""

    event: Literal["delta"] = "delta"
    item_type: Literal["assistant", "thinking"]
    text: str


RunEvent = Annotated[RunItemEvent | RunDeltaEvent, Field(discriminator="event")]


class HeartbeatEvent(BaseModel):
    """连上就发一次、之后每隔一段时间发一次：前端据此判断连接是否还活着（间隔随心跳下发），并比较前端构建的版本。"""

    type: Literal["heartbeat"] = "heartbeat"
    interval_seconds: float
    # 服务端当前前端构建的版本；包里没有前端构建时为 None
    ui_version: str | None


class ServerEvent(RootModel[TaskEvent | SavedEvent | RunItemEvent | RunDeltaEvent | HeartbeatEvent]):
    """SSE 里每条 data 的形状（api.app 的 /events）。具名是为了在 OpenAPI 里成为一个组件：前端的事件联合直接取它，
    不另列一遍（openapi-typescript 还不认识 OpenAPI 3.2 的 itemSchema）。OpenAPI 的 discriminator 不能嵌套，这里不标。"""
