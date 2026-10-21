"""issue 看板接口：界面上的操作都以用户的身份（actor 为 None）进行。"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from muliao.api.deps import C
from muliao.issues.api import KEEP, USER, ActivityKind, IssuePriority, IssueStatus, Keep, WorkState
from muliao.issues.tables import Issue, IssueActivity

router = APIRouter()


class IssueOut(BaseModel):
    id: int
    title: str
    description: str
    status: IssueStatus
    priority: IssuePriority
    # agent id、"user"，或 null（未指派）
    assignee: str | None
    # 创建者 agent id；用户创建的为 null
    creator_agent_id: str | None
    # 每个 agent 在这个 issue 上的会话：agent id -> 会话 id
    sessions: dict[str, str]
    # 被指派 agent 在这个 issue 会话里的状态；没指派给 agent 或还没有会话时为 null
    assignee_session: WorkState | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, i: Issue, state: WorkState | None) -> "IssueOut":
        return cls(
            id=i.id,
            title=i.title,
            description=i.description,
            status=i.status,
            priority=i.priority,
            assignee=USER if i.assigned_to_user else i.assignee_agent_id,
            creator_agent_id=i.creator_agent_id,
            sessions=i.sessions,
            assignee_session=state,
            created_at=i.created_at,
            updated_at=i.updated_at,
        )


class ActivityOut(BaseModel):
    id: int
    created_at: datetime
    # 做这件事的 agent id；用户为 null
    actor_agent_id: str | None
    kind: ActivityKind
    # 评论内容
    body: str | None
    # 属性变化：{"from": ..., "to": ...}；指派人的取值同 IssueOut.assignee
    change: dict[str, Any] | None

    @classmethod
    def of(cls, a: IssueActivity) -> "ActivityOut":
        return cls(
            id=a.id,
            created_at=a.created_at,
            actor_agent_id=a.actor_agent_id,
            kind=a.kind,
            body=a.body,
            change=a.change,
        )


class IssueDetailOut(BaseModel):
    issue: IssueOut
    activities: list[ActivityOut]


class IssueCreate(BaseModel):
    title: str
    description: str = ""
    status: IssueStatus = IssueStatus.BACKLOG
    priority: IssuePriority = IssuePriority.NONE
    # agent id、"user"，或 null
    assignee: str | None = None


class IssuePatch(BaseModel):
    """只修改给出的字段。assignee 给出 null 表示取消指派，不出现表示不变。"""

    title: str | None = None
    description: str | None = None
    status: IssueStatus | None = None
    priority: IssuePriority | None = None
    assignee: str | None = Field(default=None)


class CommentIn(BaseModel):
    body: str


async def _out(c: C, issue: Issue) -> IssueOut:
    return IssueOut.of(issue, (await c.issues.work_states([issue]))[issue.id])


@router.get("/issues")
async def list_issues(
    c: C, status: list[IssueStatus] | None = Query(None), assignee: str | None = None
) -> list[IssueOut]:
    found = await c.issues.list(statuses=status, assignee=assignee)
    states = await c.issues.work_states(found)
    return [IssueOut.of(i, states[i.id]) for i in found]


@router.post("/issues", status_code=201)
async def create_issue(c: C, body: IssueCreate) -> IssueOut:
    issue = await c.issues.create(
        None,
        title=body.title,
        description=body.description,
        status=body.status,
        priority=body.priority,
        assignee=body.assignee,
    )
    return await _out(c, issue)


@router.get("/issues/{issue_id}")
async def get_issue(c: C, issue_id: int) -> IssueDetailOut:
    detail = await c.issues.get(issue_id)
    return IssueDetailOut(
        issue=await _out(c, detail.issue), activities=[ActivityOut.of(a) for a in detail.activities]
    )


@router.patch("/issues/{issue_id}")
async def update_issue(c: C, issue_id: int, body: IssuePatch) -> IssueOut:
    assignee: str | None | Keep = body.assignee if "assignee" in body.model_fields_set else KEEP
    issue = await c.issues.update(
        None,
        issue_id,
        title=body.title,
        description=body.description,
        status=body.status,
        priority=body.priority,
        assignee=assignee,
    )
    return await _out(c, issue)


@router.post("/issues/{issue_id}/comments", status_code=201)
async def comment_issue(c: C, issue_id: int, body: CommentIn) -> ActivityOut:
    return ActivityOut.of(await c.issues.comment(None, issue_id, body.body))


@router.delete("/issues/{issue_id}", status_code=204)
async def delete_issue(c: C, issue_id: int) -> None:
    await c.issues.delete(issue_id)
