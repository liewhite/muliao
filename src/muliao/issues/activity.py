"""issue 时间线上的动态：每种一个类型（种类、内容 body、变化 change 的形状只在这里定义一次）。

变更单元（issues.change）只收这些类型来记动态，规则（rules）、渲染（render）与 HTTP 接口都读类型，不按种类去猜 JSON。
库里的形状（issue_activities 的 kind、body、change 三列）就是各类型按别名导出的样子：change 是 {"from": 原值, "to": 新值}，
上游完成另带 "issue"。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from muliao.issues.tables import ActivityKind, IssueActivity, IssuePriority


class _Value(BaseModel):
    # 库里多出没定义的内容也算形状不符。接口描述按输出的样子给：有默认值的（kind、Arrival 的 from）输出里总在，列为必有
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        json_schema_mode_override="serialization",
        json_schema_serialization_defaults_required=True,
    )


class Change[T](_Value):
    """属性从什么改成什么（存为 {"from": ..., "to": ...}）。"""

    before: T = Field(alias="from")
    to: T


class Arrival(_Value):
    """不是属性变化、只有一个值的记法（存为 {"from": null, "to": 值}）。"""

    before: None = Field(default=None, alias="from")
    to: str


class DependencyChange(Change[str]):
    # 完成的那个上游
    issue: int


class Created(_Value):
    kind: Literal[ActivityKind.CREATED] = ActivityKind.CREATED


class Commented(_Value):
    kind: Literal[ActivityKind.COMMENT] = ActivityKind.COMMENT
    # 评论内容
    body: str


class DescriptionEdited(_Value):
    kind: Literal[ActivityKind.DESCRIPTION] = ActivityKind.DESCRIPTION


class TitleChanged(_Value):
    kind: Literal[ActivityKind.TITLE] = ActivityKind.TITLE
    change: Change[str]


class StatusChanged(_Value):
    kind: Literal[ActivityKind.STATUS] = ActivityKind.STATUS
    # 状态是 str 不是 IssueStatus：#258 之前的历史里有已去掉的 todo、waiting，时间线只增不改
    change: Change[str]


class PriorityChanged(_Value):
    kind: Literal[ActivityKind.PRIORITY] = ActivityKind.PRIORITY
    change: Change[IssuePriority]


class AssigneeChanged(_Value):
    kind: Literal[ActivityKind.ASSIGNEE] = ActivityKind.ASSIGNEE
    # agent id、"user"，或 null（未指派）
    change: Change[str | None]


class OwnerChanged(_Value):
    kind: Literal[ActivityKind.OWNER] = ActivityKind.OWNER
    # agent id，或 null（负责的 agent 已删除）
    change: Change[str | None]


class DependsOnChanged(_Value):
    kind: Literal[ActivityKind.DEPENDS_ON] = ActivityKind.DEPENDS_ON
    # 上游 issue id 列表的前后
    change: Change[list[int]]


class DependencyFinished(_Value):
    """一个上游完成（done / cancelled）：它的状态变化，状态的取值同 StatusChanged。"""

    kind: Literal[ActivityKind.DEPENDENCY] = ActivityKind.DEPENDENCY
    change: DependencyChange


class RunFailure(_Value):
    """某个 agent 在它这个 issue 的会话里的运行最终失败了：actor 是该 agent。"""

    kind: Literal[ActivityKind.RUN_FAILED] = ActivityKind.RUN_FAILED
    # 截短的错误原文
    body: str


class Unsettled(_Value):
    """运行结束了，issue 却还是 in_progress、没人在做（rules.stalled）：请它表态。actor 是该 agent。"""

    kind: Literal[ActivityKind.UNSETTLED] = ActivityKind.UNSETTLED


class Stopped(_Value):
    """actor 用 issue_stop 停掉了被指派 agent 在这个 issue 上的会话。"""

    kind: Literal[ActivityKind.STOPPED] = ActivityKind.STOPPED
    # to 是被停掉会话的 agent id
    change: Arrival


class EventPosted(_Value):
    """投到这个 issue 的一个事件（触发器的投递目标 issue:N）：actor 是事件来源所属的 agent。"""

    kind: Literal[ActivityKind.EVENT] = ActivityKind.EVENT
    # 投递的内容
    body: str
    # to 是来源的描述，如 "trigger slack-live"
    change: Arrival


type Happening = Annotated[
    Created
    | Commented
    | DescriptionEdited
    | TitleChanged
    | StatusChanged
    | PriorityChanged
    | AssigneeChanged
    | OwnerChanged
    | DependsOnChanged
    | DependencyFinished
    | RunFailure
    | Unsettled
    | Stopped
    | EventPosted,
    Field(discriminator="kind"),
]
_HAPPENING: TypeAdapter[Happening] = TypeAdapter(Happening)
# 全部动态类型（HTTP 接口按它逐个给出响应类型）
HAPPENINGS: tuple[type[BaseModel], ...] = get_args(get_args(Happening.__value__)[0])


def stored(what: Happening) -> tuple[ActivityKind, str | None, dict[str, Any] | None]:
    """一条动态存进库的三列：种类、内容、变化（JSON）。"""
    dumped = what.model_dump(mode="json", by_alias=True)
    return what.kind, dumped.get("body"), dumped.get("change")


@dataclass(frozen=True)
class Activity:
    """时间线上记下的一条：谁、什么时候、在哪个会话里，发生了什么。"""

    id: int
    created_at: datetime
    # 做这件事的 agent id；用户为 USER，系统自己的清理为 rules.SYSTEM_ACTOR
    actor: str
    # 这件事发生在哪个会话；用户在界面上的操作、系统的清理、会话已删除时为 None
    session_id: str | None
    what: Happening

    @classmethod
    def of(cls, row: IssueActivity) -> "Activity":
        """库里的一行按种类解析；形状不符（种类缺了内容或变化）当场报错，不交给读的人去猜。"""
        fields: dict[str, Any] = {"kind": row.kind}
        if row.body is not None:
            fields["body"] = row.body
        if row.change is not None:
            fields["change"] = row.change
        return cls(
            id=row.id,
            created_at=row.created_at,
            actor=row.actor,
            session_id=row.session_id,
            what=_HAPPENING.validate_python(fields),
        )
