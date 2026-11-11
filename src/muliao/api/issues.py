"""issue 看板接口：界面上的操作都以用户的身份（actor 为 USER）进行。"""

from datetime import datetime
from typing import Annotated, Union

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, create_model

from muliao.agents.api import USER
from muliao.api.deps import C
from muliao.api.fieldlock import check_base
from muliao.issues.api import HAPPENINGS, Activity, IssuePriority, IssueStatus, IssueWork, NewIssue, WorkState
from muliao.issues.tables import Issue
from muliao.platform.values import KEEP, Keep

router = APIRouter()


class IssueOut(BaseModel):
    id: int
    title: str
    description: str
    status: IssueStatus
    priority: IssuePriority
    # 负责的项目（agent id）；只在负责的 agent 被删除后为 null
    owner: str | None
    # agent id、"user"，或 null（未指派）
    assignee: str | None
    # 创建者：agent id；用户创建的为 "user"
    creator: str
    # 每个 agent 在这个 issue 上的会话：agent id -> 会话 id
    sessions: dict[str, str]
    # 被指派 agent 在这个 issue 会话里的状态；没指派给 agent 或还没有会话时为 null
    assignee_state: WorkState | None
    # in_progress、指派给 agent，它的 issue 会话却空闲或不存在：该有人做却没人在做
    stalled: bool
    # 它依赖的上游 issue id、依赖它的下游 issue id、还没完成（不是 done / cancelled）的上游：非空时还轮不到它
    depends_on: list[int]
    blocks: list[int]
    waiting_on: list[int]
    # 已结束（done / cancelled）：不再挡着依赖它的 issue
    finished: bool
    # 用户能审核（通过或打回）：指派给用户、还没结束
    reviewable: bool
    # 用户能一键开工（指派给 owner 并进入 in_progress）：还在 backlog、有 owner
    startable: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, i: Issue, work: IssueWork) -> "IssueOut":
        return cls(
            id=i.id,
            title=i.title,
            description=i.description,
            status=i.status,
            priority=i.priority,
            owner=i.owner_agent_id,
            assignee=i.assignee,
            creator=i.creator,
            sessions=work.sessions,
            assignee_state=work.assignee_state,
            stalled=work.stalled,
            depends_on=[d.id for d in work.depends_on],
            blocks=[d.id for d in work.blocks],
            waiting_on=list(work.waiting_on),
            finished=work.finished,
            reviewable=work.reviewable,
            startable=work.startable,
            created_at=i.created_at,
            updated_at=i.updated_at,
        )


class _ActivityBase(BaseModel):
    """时间线上的一条。按 kind 区分的内容与变化由领域的动态类型定义（issues/activity.py），这里只接上共同的字段。"""

    id: int
    created_at: datetime
    # 做这件事的 agent id；用户为 "user"，系统自己的清理（删除 agent）为 "system"
    actor: str
    # 这件事发生在哪个会话（agent 动手时所在的会话）；用户在界面上的操作、系统的清理、会话已删除时为 null
    session_id: str | None


# 每种动态一个响应类型：领域类型的字段加上共同字段
_OUT: dict[type[BaseModel], type[BaseModel]] = {
    what: create_model(f"{what.__name__}Activity", __base__=(what, _ActivityBase), __doc__=what.__doc__)
    for what in HAPPENINGS
}
ActivityOut = Annotated[Union[tuple(_OUT.values())], Field(discriminator="kind")]  # noqa: UP007


def _activity_out(a: Activity) -> BaseModel:
    return _OUT[type(a.what)](
        id=a.id, created_at=a.created_at, actor=a.actor, session_id=a.session_id, **dict(a.what)
    )


class IssueDetailOut(BaseModel):
    issue: IssueOut
    activities: list[ActivityOut]


class IssueCreate(BaseModel):
    title: str
    # 负责的项目（agent id），必填
    owner: str
    # 缺省值以领域的 NewIssue 为准
    description: str = NewIssue.description
    status: IssueStatus = NewIssue.status
    priority: IssuePriority = NewIssue.priority
    # agent id、"user"，或 null
    assignee: str | None = NewIssue.assignee
    # 依赖的上游 issue id；不能成环
    depends_on: list[int] = []


class IssueFields(BaseModel):
    """issue 能改的字段（局部修改与它的 base 共用）。assignee 给出 null 表示取消指派，不出现表示不变。"""

    model_config = ConfigDict(extra="forbid")

    owner: str | None = None
    title: str | None = None
    description: str | None = None
    status: IssueStatus | None = None
    priority: IssuePriority | None = None
    assignee: str | None = Field(default=None)
    # 依赖的上游 issue id，整体替换（[] 去掉全部）；不出现表示不变
    depends_on: list[int] | None = None


class IssuePatch(IssueFields):
    """只修改给出的字段。"""

    # 改动字段开始编辑时的值（IssueOut 的形状）；此刻已不同就 409（api.fieldlock）
    base: IssueFields | None = None


class CommentIn(BaseModel):
    body: str


class RejectIn(BaseModel):
    # 打回的理由，记成评论
    reason: str


async def _out(c: C, issue: Issue) -> IssueOut:
    return IssueOut.of(issue, (await c.issues.work([issue]))[issue.id])


@router.get("/issues")
async def list_issues(
    c: C,
    status: list[IssueStatus] | None = Query(None),
    assignee: str | None = None,
    owner: str | None = None,
) -> list[IssueOut]:
    found = await c.issues.list(statuses=status, assignee=assignee, owner=owner)
    work = await c.issues.work(found)
    return [IssueOut.of(i, work[i.id]) for i in found]


@router.post("/issues", status_code=201)
async def create_issue(c: C, body: IssueCreate) -> IssueOut:
    issue = await c.issues.create(
        USER,
        owner=body.owner,
        title=body.title,
        description=body.description,
        status=body.status,
        priority=body.priority,
        assignee=body.assignee,
        depends_on=body.depends_on,
    )
    return await _out(c, issue)


@router.get("/issues/{issue_id}")
async def get_issue(c: C, issue_id: int) -> IssueDetailOut:
    detail = await c.issues.get(issue_id)
    return IssueDetailOut(
        issue=await _out(c, detail.issue), activities=[_activity_out(a) for a in detail.activities]
    )


@router.patch("/issues/{issue_id}")
async def update_issue(c: C, issue_id: int, body: IssuePatch) -> IssueOut:
    if body.base is not None:
        current = await _out(c, (await c.issues.get(issue_id)).issue)
        check_base(f"issue #{issue_id}", current.model_dump(mode="json"), body.base)
    assignee: str | None | Keep = body.assignee if "assignee" in body.model_fields_set else KEEP
    issue = await c.issues.update(
        USER,
        issue_id,
        owner=body.owner,
        title=body.title,
        description=body.description,
        status=body.status,
        priority=body.priority,
        assignee=assignee,
        depends_on=body.depends_on,
    )
    return await _out(c, issue)


@router.post("/issues/{issue_id}/comments", status_code=201)
async def comment_issue(c: C, issue_id: int, body: CommentIn) -> ActivityOut:
    return _activity_out(await c.issues.comment(USER, issue_id, body.body))


@router.post("/issues/{issue_id}/approve")
async def approve_issue(c: C, issue_id: int) -> IssueOut:
    """审核通过指派给用户的 issue：设为 done。没有指派给用户时 409。"""
    return await _out(c, await c.issues.approve(issue_id))


@router.post("/issues/{issue_id}/start")
async def start_issue(c: C, issue_id: int) -> IssueOut:
    """开工 backlog 里的 issue：指派给 owner 并进入 in_progress。不在 backlog 或没有 owner 时 409。"""
    return await _out(c, await c.issues.start(issue_id))


@router.post("/issues/{issue_id}/reject")
async def reject_issue(c: C, issue_id: int, body: RejectIn) -> IssueOut:
    """打回指派给用户的 issue：理由记成评论，回到 in_progress，指派给把它交给用户的 agent。没有指派给用户、或不是 agent 交来的时 409。"""
    return await _out(c, await c.issues.reject(issue_id, body.reason))


@router.delete("/issues/{issue_id}", status_code=204)
async def delete_issue(c: C, issue_id: int) -> None:
    await c.issues.delete(issue_id)
