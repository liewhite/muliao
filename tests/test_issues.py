"""issue 看板：修改记时间线；todo / in_progress 时每条动态投给被指派的 agent，进它在这个 issue 上唯一的会话。"""

import pytest
from sqlalchemy import select

from muliao.agents.events import AgentDeleting
from muliao.errors import NotFound
from muliao.issues.api import USER, ActivityKind, IssueStatus, WorkState
from muliao.issues.rules import Snapshot, recipient, stalled
from muliao.platform.messaging import Delivery
from muliao.platform.tooling import Origin
from muliao.sessions.tables import Mail, Session
from tests.test_flow import deliver, drain, start

TODO, BACKLOG, PROGRESS, REVIEW, DONE = (
    IssueStatus.TODO,
    IssueStatus.BACKLOG,
    IssueStatus.IN_PROGRESS,
    IssueStatus.IN_REVIEW,
    IssueStatus.DONE,
)


@pytest.mark.parametrize(
    ("issue", "from_its_session", "to"),
    [
        (Snapshot(TODO, "a"), None, "a"),
        (Snapshot(PROGRESS, "a"), "b", "a"),
        (Snapshot(PROGRESS, "a"), "a", None),
        (Snapshot(BACKLOG, "a"), None, None),
        (Snapshot(REVIEW, "a"), None, None),
        (Snapshot(DONE, "a"), None, None),
        (Snapshot(TODO, USER), None, None),
        (Snapshot(TODO, None), None, None),
    ],
    ids=[
        "active",
        "another-agents-issue-session",
        "its-own-issue-session",
        "backlog",
        "in-review",
        "done",
        "user-assignee",
        "unassigned",
    ],
)
def test_recipient_rules(issue, from_its_session, to):
    assert recipient(issue, from_its_session) == to


def test_stalled_means_an_active_agent_issue_nobody_works_on():
    assert stalled(Snapshot(TODO, "a"), None)
    assert stalled(Snapshot(PROGRESS, "a"), WorkState.IDLE)
    assert not stalled(Snapshot(PROGRESS, "a"), WorkState.RUNNING)
    assert not stalled(Snapshot(TODO, "a"), WorkState.WAITING)
    assert not stalled(Snapshot(BACKLOG, "a"), WorkState.IDLE)
    assert not stalled(Snapshot(TODO, USER), None)
    assert not stalled(Snapshot(TODO, None), None)


async def _issue_sessions(container) -> list[Session]:
    async with container.db.transaction() as s:
        query = select(Session).where(Session.origin == Origin.ISSUE).order_by(Session.created_at)
        return list((await s.scalars(query)).all())


async def _mail(container, session_id: str) -> list[str]:
    async with container.db.transaction() as s:
        rows = await s.scalars(select(Mail).where(Mail.session_id == session_id).order_by(Mail.id))
        return [m.content for m in rows]


async def test_every_change_lands_in_the_one_session_of_the_issue_and_agent(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    issue = await container.issues.create(
        None, owner=alpha, title="Fix the build", description="CI is red", assignee=alpha
    )
    await deliver(container)
    assert await _issue_sessions(container) == []  # backlog：不投

    await container.issues.update(None, issue.id, status=TODO)
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert (session.agent_id, session.origin_ref) == (alpha, str(issue.id))
    [mail] = await _mail(container, session.id)
    assert "user changed status: backlog -> todo" in mail and "CI is red" in mail

    # 评论、改派走又改派回来、改状态、改标题：同一个 agent 始终在同一个会话里接着做
    await container.issues.comment(None, issue.id, "any update?")
    await container.issues.update(None, issue.id, assignee=beta)
    await container.issues.update(None, issue.id, assignee=alpha, status=PROGRESS)
    await container.issues.comment(beta, issue.id, "handing it back")
    await container.issues.update(None, issue.id, title="Fix the build now")
    await deliver(container)
    sessions = await _issue_sessions(container)
    assert [(s.agent_id, s.origin_ref) for s in sessions] == [(alpha, str(issue.id)), (beta, str(issue.id))]
    mails = await _mail(container, session.id)
    assert len(mails) == 5
    assert "any update?" in mails[1]
    assert (
        "user changed assignee: beta -> alpha" in mails[2]
        and "user changed status: todo -> in_progress" in mails[2]
    )
    assert "beta commented:\n  handing it back" in mails[3].split("\n\n")[0]
    assert "Fix the build now" in mails[4]
    assert (await container.issues.work([issue]))[issue.id].sessions == {
        alpha: session.id,
        beta: sessions[1].id,
    }


async def test_changes_before_delivery_still_create_only_one_session(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    await container.issues.comment(None, issue.id, "first")
    await container.issues.comment(None, issue.id, "second")
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert len(await _mail(container, session.id)) == 3


async def test_an_agent_acting_in_its_issue_session_does_not_deliver_to_itself(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    await drain(container)
    [session] = await _issue_sessions(container)
    await container.issues.update(alpha, issue.id, session_id=session.id, status=PROGRESS)
    await container.issues.comment(alpha, issue.id, "working on it", session_id=session.id)
    await deliver(container)
    assert len(await _mail(container, session.id)) == 1


async def test_an_agent_assigning_itself_from_a_chat_starts_the_issue_session(container, make_agent):
    alpha = (await make_agent()).id
    chat = await start(container, alpha, "track this")
    issue = await container.issues.create(
        alpha, owner=alpha, session_id=chat.id, title="mine", status=TODO, assignee=alpha
    )
    await deliver(container)
    # issue 只在它自己的会话里做：普通对话只建、派
    [session] = await _issue_sessions(container)
    assert session.agent_id == alpha and session.id != chat.id
    assert "alpha created the issue" in (await _mail(container, session.id))[0]

    # 它在普通对话里评论同样投进 issue 会话
    await container.issues.comment(alpha, issue.id, "one more thing", session_id=chat.id)
    await deliver(container)
    assert "one more thing" in (await _mail(container, session.id))[-1]


async def test_backlog_review_and_done_are_not_delivered(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t", assignee=alpha)
    await container.issues.comment(None, issue.id, "later")
    await container.issues.update(None, issue.id, status=REVIEW)
    await container.issues.comment(None, issue.id, "looks good")
    await container.issues.update(None, issue.id, status=DONE)
    await deliver(container)
    assert await _issue_sessions(container) == []


async def test_handing_over_delivers_to_the_new_assignee_and_records_the_timeline(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    issue = await container.issues.create(alpha, owner=alpha, title="t", assignee=alpha)
    await container.issues.update(alpha, issue.id, assignee=beta, status=PROGRESS)
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert session.agent_id == beta
    assert "alpha changed assignee: alpha -> beta" in (await _mail(container, session.id))[0]

    detail = await container.issues.get(issue.id)
    kinds = [a.kind for a in detail.activities]
    assert kinds == [ActivityKind.CREATED, ActivityKind.STATUS, ActivityKind.ASSIGNEE]
    assert detail.activities[2].change == {"from": alpha, "to": beta}


async def test_an_agent_handing_an_issue_to_the_user_notifies_them(container, make_agent):
    from muliao.notifications.tables import Notification

    alpha = (await make_agent()).id
    chat = await start(container, alpha, "work")
    issue = await container.issues.create(None, owner=alpha, title="Review me", status=TODO)
    await container.issues.update(alpha, issue.id, session_id=chat.id, status=REVIEW, assignee=USER)
    await deliver(container)
    async with container.db.transaction() as s:
        [note] = (await s.scalars(select(Notification))).all()
    assert note.title.startswith(f"#{issue.id} ") and note.session_id == chat.id


async def test_a_deleted_issue_session_is_replaced_on_the_next_delivery(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    await drain(container)  # 跑完这次运行，会话才能删除
    [first] = await _issue_sessions(container)
    await container.sessions.delete(first.id)
    await deliver(container)

    await container.issues.comment(None, issue.id, "again")
    await deliver(container)
    [second] = await _issue_sessions(container)
    assert second.id != first.id


async def test_deleting_an_agent_unassigns_its_issues(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    await deliver(container)
    commit = await container.issues.on_agent_deleting(AgentDeleting(agent_id=alpha), Delivery("m", 0))
    async with container.db.transaction() as s:
        await commit(s)
    detail = await container.issues.get(issue.id)
    assert detail.issue.assignee is None
    assert detail.activities[-1].change == {"from": alpha, "to": None}


async def test_assigning_to_an_unknown_agent_is_refused(container, make_agent):
    alpha = (await make_agent()).id
    with pytest.raises(NotFound):
        await container.issues.create(None, owner=alpha, title="t", assignee="nobody")


async def test_http_issue_lifecycle(client, container, make_agent):
    from tests.conftest import login

    await login(client)
    alpha = (await make_agent()).id
    resp = await client.post("/api/issues", json={"title": "Write docs", "owner": alpha, "assignee": alpha})
    assert resp.status_code == 201
    assert resp.json()["stalled"] is False and resp.json()["sessions"] == {}
    issue_id = resp.json()["id"]

    resp = await client.patch(f"/api/issues/{issue_id}", json={"status": "todo"})
    assert resp.json()["status"] == "todo" and resp.json()["assignee"] == alpha
    await deliver(container)
    detail = (await client.get(f"/api/issues/{issue_id}")).json()
    assert alpha in detail["issue"]["sessions"]
    assert detail["issue"]["assignee_session"] == "running" and detail["issue"]["stalled"] is False
    assert [a["kind"] for a in detail["activities"]] == ["created", "status"]

    await drain(container)
    listed = (await client.get("/api/issues", params={"status": ["todo"]})).json()
    assert [(i["id"], i["assignee_session"], i["stalled"]) for i in listed] == [(issue_id, "idle", True)]

    # 只给出的字段改变；assignee 为 null 取消指派
    resp = await client.patch(f"/api/issues/{issue_id}", json={"assignee": None})
    assert resp.json()["assignee"] is None and resp.json()["status"] == "todo" and not resp.json()["stalled"]
    resp = await client.patch(f"/api/issues/{issue_id}", json={"assignee": "user"})
    assert resp.json()["assignee"] == "user"
    assert [i["id"] for i in (await client.get("/api/issues", params={"assignee": "user"})).json()] == [
        issue_id
    ]
    assert (await client.post(f"/api/issues/{issue_id}/comments", json={"body": "hi"})).status_code == 201

    assert (await client.delete(f"/api/issues/{issue_id}")).status_code == 204
    assert (await client.get(f"/api/issues/{issue_id}")).status_code == 404


async def test_work_of_the_assignee_session(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    unassigned = await container.issues.create(None, owner=alpha, title="u", status=TODO)
    await deliver(container)
    work = await container.issues.work(await container.issues.list())
    assert (work[issue.id].assignee_session, work[issue.id].stalled) == (WorkState.RUNNING, False)
    assert (work[unassigned.id].assignee_session, work[unassigned.id].stalled) == (None, False)
    await drain(container)
    work = await container.issues.work(await container.issues.list())
    assert (work[issue.id].assignee_session, work[issue.id].stalled) == (WorkState.IDLE, True)


async def test_the_owner_stays_when_the_issue_is_reassigned_and_filters_a_project(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    issue = await container.issues.create(alpha, owner=alpha, title="t", assignee=alpha)
    other = await container.issues.create(None, owner=beta, title="u")
    await container.issues.update(alpha, issue.id, assignee=beta)
    assert [i.id for i in await container.issues.list(owner=alpha)] == [issue.id]
    assert [i.id for i in await container.issues.list(owner=beta)] == [other.id]

    await container.issues.update(None, issue.id, owner=beta)
    detail = await container.issues.get(issue.id)
    assert detail.activities[-1].kind == ActivityKind.OWNER
    assert "user changed owner: alpha -> beta" in await container.issues.render(issue.id)

    with pytest.raises(NotFound):
        await container.issues.create(None, owner="nobody", title="x")


async def test_deleting_the_owner_agent_clears_ownership(container, make_agent):
    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="t")
    commit = await container.issues.on_agent_deleting(AgentDeleting(agent_id=alpha), Delivery("m", 0))
    async with container.db.transaction() as s:
        await commit(s)
    assert (await container.issues.get(issue.id)).issue.owner_agent_id is None
