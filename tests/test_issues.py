"""issue 看板：修改记时间线，按规则唤醒被指派的 agent（续写它在这个 issue 上的会话）。"""

import pytest
from sqlalchemy import select

from muliao.agents.events import AgentDeleting
from muliao.errors import NotFound
from muliao.issues.api import USER, ActivityKind, IssueStatus
from muliao.issues.rules import Snapshot, comment_wakes, wake_target
from muliao.platform.messaging import Delivery
from muliao.platform.tooling import Origin
from muliao.sessions.tables import Mail, Session
from tests.test_flow import deliver, drain

TODO, BACKLOG, PROGRESS, REVIEW = (
    IssueStatus.TODO,
    IssueStatus.BACKLOG,
    IssueStatus.IN_PROGRESS,
    IssueStatus.IN_REVIEW,
)


@pytest.mark.parametrize(
    ("before", "after", "actor", "woken"),
    [
        (None, Snapshot(TODO, "a"), None, "a"),
        (None, Snapshot(BACKLOG, "a"), None, None),
        (Snapshot(BACKLOG, "a"), Snapshot(TODO, "a"), None, "a"),
        (Snapshot(REVIEW, "a"), Snapshot(PROGRESS, "a"), "b", "a"),
        (Snapshot(TODO, "a"), Snapshot(PROGRESS, "a"), None, None),
        (Snapshot(TODO, None), Snapshot(TODO, "a"), None, "a"),
        (Snapshot(PROGRESS, "b"), Snapshot(PROGRESS, "a"), "b", "a"),
        (Snapshot(BACKLOG, "a"), Snapshot(TODO, "a"), "a", None),  # 在它自己的 issue 会话里
        (Snapshot(BACKLOG, None), Snapshot(TODO, USER), None, None),
        (Snapshot(TODO, "a"), Snapshot(REVIEW, "a"), None, None),
    ],
    ids=[
        "created-active",
        "created-backlog",
        "moved-into-todo",
        "reopened-by-other-agent",
        "already-active",
        "assigned-while-active",
        "handed-over",
        "working-in-its-session",
        "user-assignee",
        "moved-out",
    ],
)
def test_wake_rules(before, after, actor, woken):
    assert wake_target(before, after, actor) == woken


def test_comments_wake_the_assignee_of_an_active_issue_only():
    assert comment_wakes(Snapshot(TODO, "a"), None) == "a"
    assert comment_wakes(Snapshot(TODO, "a"), "a") is None
    assert comment_wakes(Snapshot(BACKLOG, "a"), None) is None
    assert comment_wakes(Snapshot(TODO, USER), "a") is None


async def _issue_sessions(container) -> list[Session]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Session).where(Session.origin == Origin.ISSUE))).all())


async def _mail(container, session_id: str) -> list[str]:
    async with container.db.transaction() as s:
        rows = await s.scalars(select(Mail).where(Mail.session_id == session_id).order_by(Mail.id))
        return [m.content for m in rows]


async def test_moving_an_assigned_issue_to_todo_wakes_the_agent_in_one_session(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(
        None, title="Fix the build", description="CI is red", assignee=alpha
    )
    await deliver(container)
    assert await _issue_sessions(container) == []  # backlog：不唤醒

    await container.issues.update(None, issue.id, status=IssueStatus.TODO)
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert (session.agent_id, session.origin_ref) == (alpha, str(issue.id))
    [mail] = await _mail(container, session.id)
    assert "moved from backlog to todo" in mail and "CI is red" in mail

    # 进行中时用户评论：续写同一个会话
    await container.issues.comment(None, issue.id, "any update?")
    await deliver(container)
    assert [s.id for s in await _issue_sessions(container)] == [session.id]
    assert "any update?" in (await _mail(container, session.id))[-1]


async def test_an_agent_working_in_the_issue_session_does_not_wake_itself(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, title="t", status=IssueStatus.TODO, assignee=alpha)
    await drain(container)
    [session] = await _issue_sessions(container)
    # 在这个 issue 自己的会话里改状态、评论：它正在处理，不再唤醒
    await container.issues.update(alpha, issue.id, session_id=session.id, status=IssueStatus.IN_REVIEW)
    await container.issues.update(alpha, issue.id, session_id=session.id, status=IssueStatus.IN_PROGRESS)
    await container.issues.comment(alpha, issue.id, "working on it", session_id=session.id)
    await deliver(container)
    assert len(await _mail(container, session.id)) == 1


async def test_an_agent_assigning_itself_from_another_session_is_woken(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(
        alpha, session_id="elsewhere", title="later", status=IssueStatus.TODO, assignee=alpha
    )
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert (session.agent_id, session.origin_ref) == (alpha, str(issue.id))


async def test_handing_over_wakes_the_new_assignee_and_records_the_timeline(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    issue = await container.issues.create(alpha, title="t", assignee=alpha)
    await container.issues.update(alpha, issue.id, assignee=beta, status=IssueStatus.IN_PROGRESS)
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert session.agent_id == beta
    assert "assigned to you" in (await _mail(container, session.id))[0]

    detail = await container.issues.get(issue.id)
    kinds = [a.kind for a in detail.activities]
    assert kinds == [ActivityKind.CREATED, ActivityKind.STATUS, ActivityKind.ASSIGNEE]
    assert detail.activities[2].change == {"from": alpha, "to": beta}
    assert "alpha changed assignee: alpha -> beta" in await container.issues.render(issue.id)


async def test_an_agent_handing_an_issue_to_the_user_notifies_them(container, make_agent):
    from muliao.notifications.tables import Notification

    alpha = (await make_agent()).id
    issue = await container.issues.create(None, title="Review me", status=IssueStatus.TODO)
    await container.issues.update(alpha, issue.id, status=IssueStatus.IN_REVIEW, assignee=USER)
    await deliver(container)
    async with container.db.transaction() as s:
        [note] = (await s.scalars(select(Notification))).all()
    assert note.title.startswith(f"#{issue.id} ")


async def test_a_deleted_issue_session_is_replaced_on_the_next_wake(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, title="t", status=IssueStatus.TODO, assignee=alpha)
    await deliver(container)
    await drain(container)  # 跑完这次运行，会话才能删除
    [first] = await _issue_sessions(container)
    await container.sessions.delete(first.id)
    await deliver(container)

    await container.issues.update(None, issue.id, status=IssueStatus.BACKLOG)
    await container.issues.update(None, issue.id, status=IssueStatus.TODO)
    await deliver(container)
    [second] = await _issue_sessions(container)
    assert second.id != first.id


async def test_deleting_an_agent_unassigns_its_issues(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, title="t", status=IssueStatus.TODO, assignee=alpha)
    await deliver(container)
    commit = await container.issues.on_agent_deleting(AgentDeleting(agent_id=alpha), Delivery("m", 0))
    async with container.db.transaction() as s:
        await commit(s)
    detail = await container.issues.get(issue.id)
    assert detail.issue.assignee_agent_id is None and detail.issue.sessions == {}
    assert detail.activities[-1].change == {"from": alpha, "to": None}


async def test_assigning_to_an_unknown_agent_is_refused(container):
    with pytest.raises(NotFound):
        await container.issues.create(None, title="t", assignee="nobody")


async def test_http_issue_lifecycle(client, container, make_agent):
    from tests.conftest import login

    await login(client)
    alpha = (await make_agent()).id
    resp = await client.post("/api/issues", json={"title": "Write docs", "assignee": alpha})
    assert resp.status_code == 201
    issue_id = resp.json()["id"]

    resp = await client.patch(f"/api/issues/{issue_id}", json={"status": "todo"})
    assert resp.json()["status"] == "todo" and resp.json()["assignee"] == alpha
    await deliver(container)
    detail = (await client.get(f"/api/issues/{issue_id}")).json()
    assert alpha in detail["issue"]["sessions"]
    assert [a["kind"] for a in detail["activities"]] == ["created", "status"]

    # 只给出的字段改变；assignee 为 null 取消指派
    resp = await client.patch(f"/api/issues/{issue_id}", json={"assignee": None})
    assert resp.json()["assignee"] is None and resp.json()["status"] == "todo"
    assert (await client.post(f"/api/issues/{issue_id}/comments", json={"body": "hi"})).status_code == 201

    listed = (await client.get("/api/issues", params={"status": ["todo"]})).json()
    assert [i["id"] for i in listed] == [issue_id]
    assert (await client.delete(f"/api/issues/{issue_id}")).status_code == 204
    assert (await client.get(f"/api/issues/{issue_id}")).status_code == 404


async def test_work_state_of_the_assignee_session(container, make_agent):
    from muliao.issues.api import WorkState

    alpha = (await make_agent()).id
    issue = await container.issues.create(None, title="t", status=IssueStatus.TODO, assignee=alpha)
    unassigned = await container.issues.create(None, title="u", status=IssueStatus.TODO)
    await deliver(container)
    states = await container.issues.work_states(await container.issues.list())
    assert states == {issue.id: WorkState.RUNNING, unassigned.id: None}  # 运行排着
    await drain(container)
    states = await container.issues.work_states(await container.issues.list())
    assert states[issue.id] == WorkState.IDLE  # 运行结束了，issue 还在 todo：没人在做
