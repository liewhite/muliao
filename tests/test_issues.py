"""issue 看板：修改记时间线；todo / in_progress 时每条动态投给被指派的 agent，进它在这个 issue 上唯一的会话。"""

from pathlib import Path

import pytest
from sqlalchemy import select

from muliao.agent_runtime.instructions import build_instructions
from muliao.agents.events import AgentDeleting
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.issues.api import USER, ActivityKind, IssueStatus, WorkState
from muliao.issues.rules import SYSTEM_ACTOR, Snapshot, cycle_through, handed_by, recipient, stalled
from muliao.platform.messaging import Delivery
from muliao.sessions.api import Origin
from muliao.sessions.origins import origin_note
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
        (Snapshot(TODO, "a", ()), None, "a"),
        (Snapshot(PROGRESS, "a", ()), "b", "a"),
        (Snapshot(PROGRESS, "a", ()), "a", None),
        (Snapshot(BACKLOG, "a", ()), None, None),
        (Snapshot(REVIEW, "a", ()), None, None),
        (Snapshot(DONE, "a", ()), None, None),
        (Snapshot(TODO, USER, ()), None, None),
        (Snapshot(TODO, None, ()), None, None),
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
    assert stalled(Snapshot(TODO, "a", ()), None)
    assert stalled(Snapshot(PROGRESS, "a", ()), WorkState.IDLE)
    assert not stalled(Snapshot(PROGRESS, "a", ()), WorkState.RUNNING)
    assert not stalled(Snapshot(TODO, "a", ()), WorkState.WAITING)
    assert not stalled(Snapshot(BACKLOG, "a", ()), WorkState.IDLE)
    assert not stalled(Snapshot(TODO, USER, ()), None)
    assert not stalled(Snapshot(TODO, None, ()), None)


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
    # 信里只有一行摘要和这次的动态：描述、完整时间线由 agent 自己 issue_show
    assert mail.startswith(
        f"New activity on issue #{issue.id}: Fix the build | status: todo | assignee: alpha\n"
    )
    assert "user changed status: backlog -> todo" in mail and "CI is red" not in mail
    assert f"`issue_show({issue.id})`" in mail

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
    # 信件不随时间线变长：只带这次的动态，不重复之前的
    assert "any update?" in mails[1] and "backlog -> todo" not in mails[1]
    assert (
        "user changed assignee: beta -> alpha" in mails[2]
        and "user changed status: todo -> in_progress" in mails[2]
    )
    assert "beta commented:\n  handing it back" in mails[3].split("\n\n")[0]
    assert "any update?" not in mails[3]
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
    mails = await _mail(container, session.id)
    assert ["created the issue" in mails[0], "first" in mails[1], "second" in mails[2]] == [True] * 3


async def test_a_long_issue_title_is_shortened_to_fit_the_session_title(container, make_agent):
    """issue 标题可以比会话标题的列宽长：截断由新建会话的入口统一做，issue 这边原样给出。"""
    from muliao.sessions.tables import TITLE_LENGTH

    alpha = (await make_agent()).id
    issue = await container.issues.create(None, owner=alpha, title="x" * 300, status=TODO, assignee=alpha)
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert len(session.title) == TITLE_LENGTH
    assert session.title.startswith(f"#{issue.id} xxx")
    assert session.title.endswith("x…")


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
    # 点开打开 issue（在那里审核）
    assert note.issue_id == issue.id


def test_the_agent_that_handed_an_issue_to_the_user_is_the_last_assigner():
    assert handed_by("creator", ["a", "b"]) == "b"
    # 创建时就指派给了用户
    assert handed_by("creator", []) == "creator"
    # 用户自己、系统做的交接：没有可以打回的 agent
    assert handed_by("creator", ["a", None]) is None
    assert handed_by(None, []) is None
    assert handed_by("creator", [SYSTEM_ACTOR]) is None


async def _handed_to_user(container, agent: str):
    """agent 在它的 issue 会话里做完，交给用户审核。"""
    issue = await container.issues.create(None, owner=agent, title="Ship it", status=TODO, assignee=agent)
    await drain(container)
    [session] = await _issue_sessions(container)
    await container.issues.update(agent, issue.id, session_id=session.id, status=REVIEW, assignee=USER)
    await drain(container)
    return issue, session


async def test_approving_an_issue_handed_to_the_user_finishes_it_and_unblocks_downstream(
    container, make_agent
):
    alpha = (await make_agent("alpha")).id
    issue, _ = await _handed_to_user(container, alpha)
    downstream = await container.issues.create(
        None, owner=alpha, title="next", status=TODO, assignee=alpha, depends_on=[issue.id]
    )

    approved = await container.issues.approve(issue.id)

    assert (approved.status, approved.assignee) == (DONE, USER)
    last = (await container.issues.get(issue.id)).activities[-1]
    # 以用户的身份记在时间线上
    assert (last.actor_agent_id, last.kind, last.change) == (
        None,
        ActivityKind.STATUS,
        {"from": REVIEW, "to": DONE},
    )
    assert (await container.issues.get(downstream.id)).activities[-1].kind == ActivityKind.DEPENDENCY
    # 连点、另一台设备再点一次：什么都不变
    await container.issues.approve(issue.id)
    assert (await container.issues.get(issue.id)).activities[-1].id == last.id
    # 已经打回给 agent：不能再通过
    await container.issues.update(None, issue.id, assignee=alpha)
    with pytest.raises(Conflict):
        await container.issues.approve(issue.id)


async def test_sending_an_issue_back_comments_and_returns_it_to_the_agent_that_handed_it_over(
    container, make_agent
):
    alpha = (await make_agent("alpha")).id
    issue, session = await _handed_to_user(container, alpha)
    before = len(await _mail(container, session.id))

    with pytest.raises(InvalidInput):
        await container.issues.reject(issue.id, "  ")
    rejected = await container.issues.reject(issue.id, "the button is misaligned")
    await deliver(container)

    assert (rejected.status, rejected.assignee) == (TODO, alpha)
    activities = (await container.issues.get(issue.id)).activities[-3:]
    assert [(a.actor_agent_id, a.kind) for a in activities] == [
        (None, ActivityKind.COMMENT),
        (None, ActivityKind.STATUS),
        (None, ActivityKind.ASSIGNEE),
    ]
    assert activities[0].body == "the button is misaligned"
    # 三条动态一封信，投进 agent 原来的 issue 会话
    mails = await _mail(container, session.id)
    assert len(mails) == before + 1
    assert "the button is misaligned" in mails[-1] and "in_review -> todo" in mails[-1]
    assert [s.id for s in await _issue_sessions(container)] == [session.id]
    with pytest.raises(Conflict):
        await container.issues.reject(issue.id, "again")


async def test_an_issue_the_user_assigned_to_themselves_cannot_be_sent_back(container, make_agent):
    alpha = (await make_agent("alpha")).id
    issue = await container.issues.create(None, owner=alpha, title="mine", status=REVIEW, assignee=USER)

    with pytest.raises(Conflict, match="not handed to you by an agent"):
        await container.issues.reject(issue.id, "why")
    assert (await container.issues.get(issue.id)).issue.status == REVIEW


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
    before = (await container.issues.get(issue.id)).issue.updated_at
    commit = await container.issues.on_agent_deleting(AgentDeleting(agent_id=alpha), Delivery("m"))
    async with container.db.transaction() as s:
        await commit(s)
    detail = await container.issues.get(issue.id)
    assert detail.issue.assignee is None
    assert detail.activities[-1].change == {"from": alpha, "to": None}
    # 清理是系统做的，不是用户
    assert detail.activities[-1].actor_agent_id == SYSTEM_ACTOR
    assert detail.issue.updated_at > before
    assert "system changed assignee: alpha -> unassigned" in await container.issues.render(issue.id)


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
    # 审核：用户自己指派给自己的没有 agent 可以打回
    resp = await client.post(f"/api/issues/{issue_id}/reject", json={"reason": "no"})
    assert resp.status_code == 409
    resp = await client.post(f"/api/issues/{issue_id}/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "done"

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
    commit = await container.issues.on_agent_deleting(AgentDeleting(agent_id=alpha), Delivery("m"))
    async with container.db.transaction() as s:
        await commit(s)
    detail = await container.issues.get(issue.id)
    assert detail.issue.owner_agent_id is None
    assert (detail.activities[-1].kind, detail.activities[-1].actor_agent_id) == (
        ActivityKind.OWNER,
        SYSTEM_ACTOR,
    )


async def test_acting_from_the_session_of_another_issue_is_delivered(container, make_agent):
    alpha = (await make_agent()).id
    x = await container.issues.create(None, owner=alpha, title="x", status=TODO, assignee=alpha)
    y = await container.issues.create(None, owner=alpha, title="y", status=TODO, assignee=alpha)
    await drain(container)
    sessions = {s.origin_ref: s.id for s in await _issue_sessions(container)}
    # 在 x 的会话里评论 y：不是 y 自己的会话，投进 y 的会话
    await container.issues.comment(alpha, y.id, "from x", session_id=sessions[str(x.id)])
    await deliver(container)
    assert "from x" in (await _mail(container, sessions[str(y.id)]))[-1]
    assert len(await _mail(container, sessions[str(x.id)])) == 1


# ---- 依赖（DAG）----


def test_an_issue_waiting_on_upstream_is_neither_delivered_nor_stalled():
    waiting = Snapshot(TODO, "a", (3,))
    assert recipient(waiting, None) is None
    assert not stalled(waiting, None)


@pytest.mark.parametrize(
    ("issue_id", "depends_on", "edges", "cycle"),
    [
        (1, [1], [], [1, 1]),
        (1, [2], [(2, 1)], [1, 2, 1]),
        (1, [2], [(2, 3), (3, 1)], [1, 2, 3, 1]),
        (1, [2, 3], [(2, 3)], None),
        # 自己原有的上游正被替换，不算
        (1, [2], [(1, 3), (3, 2)], None),
        (1, [2], [(4, 1), (2, 4)], [1, 2, 4, 1]),
    ],
    ids=["self", "two-cycle", "long-cycle", "diamond", "own-edges-replaced", "through-other"],
)
def test_cycle_through(issue_id, depends_on, edges, cycle):
    assert cycle_through(issue_id, depends_on, edges) == cycle


async def test_a_downstream_issue_is_delivered_when_its_last_upstream_finishes(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    up1 = await container.issues.create(None, owner=alpha, title="up1", status=TODO, assignee=alpha)
    up2 = await container.issues.create(None, owner=alpha, title="up2", status=TODO)
    down = await container.issues.create(
        None, owner=beta, title="down", status=TODO, assignee=beta, depends_on=[up1.id, up2.id]
    )
    await container.issues.comment(None, down.id, "wait for them")
    await drain(container)
    assert {s.origin_ref for s in await _issue_sessions(container)} == {str(up1.id)}
    work = (await container.issues.work([down]))[down.id]
    assert (work.waiting_on, work.stalled) == ((up1.id, up2.id), False)
    assert [d.id for d in (await container.issues.work([up1]))[up1.id].blocks] == [down.id]

    # 上游 agent 在自己的 issue 会话里完成：不是下游的会话，照常投给下游；但还在等 up2，只记时间线
    [up1_session] = await _issue_sessions(container)
    await container.issues.update(alpha, up1.id, session_id=up1_session.id, status=DONE)
    await deliver(container)
    assert {s.origin_ref for s in await _issue_sessions(container)} == {str(up1.id)}
    detail = await container.issues.get(down.id)
    assert detail.activities[-1].kind == ActivityKind.DEPENDENCY
    assert detail.activities[-1].change == {"issue": up1.id, "from": "todo", "to": "done"}

    # 最后一个上游取消也算完成：投给下游，信里是摘要（含上游及其状态）与这次的动态；等待期间的评论靠 issue_show 看
    await container.issues.update(None, up2.id, status=IssueStatus.CANCELLED)
    await deliver(container)
    [session] = [s for s in await _issue_sessions(container) if s.origin_ref == str(down.id)]
    [mail] = await _mail(container, session.id)
    news = mail.split("\n\n")[0]
    assert f"user changed status of dependency #{up2.id}: todo -> cancelled" in news
    assert f"depends on: #{up1.id} (done), #{up2.id} (cancelled)" in news and "wait for them" not in mail
    assert (await container.issues.work([down]))[down.id].waiting_on == ()

    # 已完成的上游再改到另一个完成状态不再通知
    await container.issues.update(None, up2.id, status=DONE)
    await deliver(container)
    assert len(await _mail(container, session.id)) == 1


async def test_dependencies_must_exist_and_not_form_a_cycle(container, make_agent):
    alpha = (await make_agent()).id
    a = await container.issues.create(None, owner=alpha, title="a")
    b = await container.issues.create(None, owner=alpha, title="b", depends_on=[a.id])
    c = await container.issues.create(None, owner=alpha, title="c", depends_on=[b.id])
    with pytest.raises(InvalidInput, match=f"#{a.id} -> #{c.id} -> #{b.id} -> #{a.id}"):
        await container.issues.update(None, a.id, depends_on=[c.id])
    with pytest.raises(InvalidInput, match="cycle"):
        await container.issues.update(None, a.id, depends_on=[a.id])
    with pytest.raises(InvalidInput, match="not found"):
        await container.issues.update(None, a.id, depends_on=[999])
    with pytest.raises(InvalidInput, match="more than once"):
        await container.issues.update(None, c.id, depends_on=[a.id, a.id])
    assert (await container.issues.work([a]))[a.id].depends_on == []

    # 整体替换，记一条时间线；没变不记
    await container.issues.update(None, c.id, depends_on=[a.id, b.id])
    await container.issues.update(None, c.id, depends_on=[b.id, a.id])
    detail = await container.issues.get(c.id)
    assert [x.kind for x in detail.activities] == [ActivityKind.CREATED, ActivityKind.DEPENDS_ON]
    assert detail.activities[-1].change == {"from": [b.id], "to": [a.id, b.id]}
    assert f"user changed depends_on: #{b.id} -> #{a.id}, #{b.id}" in await container.issues.render(c.id)


async def test_dropping_the_last_waiting_upstream_delivers(container, make_agent):
    alpha = (await make_agent()).id
    up = await container.issues.create(None, owner=alpha, title="up")
    down = await container.issues.create(
        None, owner=alpha, title="down", status=TODO, assignee=alpha, depends_on=[up.id]
    )
    await deliver(container)
    assert await _issue_sessions(container) == []
    await container.issues.update(None, down.id, depends_on=[])
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert f"changed depends_on: #{up.id} -> none" in (await _mail(container, session.id))[0]


async def test_deleting_an_upstream_frees_its_downstream(container, make_agent):
    alpha = (await make_agent()).id
    up = await container.issues.create(None, owner=alpha, title="up")
    down = await container.issues.create(
        None, owner=alpha, title="down", status=TODO, assignee=alpha, depends_on=[up.id]
    )
    await container.issues.delete(up.id)
    await deliver(container)
    [session] = await _issue_sessions(container)
    assert session.origin_ref == str(down.id)
    assert (await container.issues.work([down]))[down.id].depends_on == []


async def test_http_dependencies(client, container, make_agent):
    from tests.conftest import login

    await login(client)
    alpha = (await make_agent()).id
    up = (await client.post("/api/issues", json={"title": "up", "owner": alpha})).json()["id"]
    resp = await client.post("/api/issues", json={"title": "down", "owner": alpha, "depends_on": [up]})
    down = resp.json()
    assert (down["depends_on"], down["waiting_on"], down["blocks"]) == ([up], [up], [])
    assert (await client.get(f"/api/issues/{up}")).json()["issue"]["blocks"] == [down["id"]]

    resp = await client.patch(f"/api/issues/{up}", json={"depends_on": [down["id"]]})
    assert resp.status_code == 422 and "cycle" in resp.text
    await client.patch(f"/api/issues/{up}", json={"status": "done"})
    assert (await client.get(f"/api/issues/{down['id']}")).json()["issue"]["waiting_on"] == []
    resp = await client.patch(f"/api/issues/{down['id']}", json={"depends_on": []})
    assert resp.json()["depends_on"] == []


async def test_waiting_is_derived_from_the_current_upstream_state(container, make_agent):
    alpha = (await make_agent()).id
    done = await container.issues.create(None, owner=alpha, title="done", status=DONE)
    up = await container.issues.create(None, owner=alpha, title="up")
    other = await container.issues.create(None, owner=alpha, title="other")
    # 上游创建时已经完成：立即投递
    ready = await container.issues.create(
        None, owner=alpha, title="ready", status=TODO, assignee=alpha, depends_on=[done.id]
    )
    down = await container.issues.create(None, owner=alpha, title="down", depends_on=[up.id, other.id])
    # 等待期间改状态、改指派：只记时间线
    await container.issues.update(None, down.id, status=TODO, assignee=alpha)
    await container.issues.delete(other.id)  # 还在等 up
    await deliver(container)
    assert [s.origin_ref for s in await _issue_sessions(container)] == [str(ready.id)]
    assert (await container.issues.get(down.id)).activities[-1].kind == ActivityKind.DEPENDS_ON

    await container.issues.update(None, up.id, status=DONE)
    await container.issues.update(None, up.id, status=TODO)  # 重新打开：下游回到等待
    await drain(container)
    [_, session] = await _issue_sessions(container)
    assert session.origin_ref == str(down.id)
    work = (await container.issues.work([down]))[down.id]
    assert (work.waiting_on, work.stalled) == ((up.id,), False)
    await container.issues.comment(None, down.id, "not yet")
    await deliver(container)
    assert len(await _mail(container, session.id)) == 1


def test_issue_sessions_are_told_once_how_to_work_on_the_issue():
    """处理指引在 issue 会话的固定说明里（缓存前缀），信件不重复它：说明里要让 agent 先 issue_show、做完交审。"""

    def built(origin: Origin) -> str:
        return build_instructions(
            agent_name="a",
            agent_description="",
            public_instructions="",
            agent_instructions="",
            workspace=Path("/w"),
            origin_note=origin_note(origin),
            memory="",
            skills=(),
            corrupt_skills=(),
            memory_limit=100,
            summary=None,
        )

    issue = built(Origin.ISSUE)
    assert "`issue_show`" in issue and "`in_review` and assignee `user`" in issue
    assert "set it `done` yourself" in issue and "under `blocks`" in issue
    assert "issue_show" not in built(Origin.USER)


def _failing(status_code: int, times: int):
    """前 times 次请求以这个 HTTP 状态失败，之后回答 ok。"""
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelResponse, TextPart

    from tests.scripted import scripted

    calls = {"n": 0}

    def fn(_messages, _info):
        calls["n"] += 1
        if calls["n"] <= times:
            raise ModelHTTPError(status_code=status_code, model_name="m", body="boom " * 200)
        return ModelResponse(parts=[TextPart("ok")])

    return scripted(fn)


async def _notifications(container):
    from muliao.notifications.tables import Notification

    async with container.db.transaction() as s:
        return list((await s.scalars(select(Notification).order_by(Notification.id))).all())


async def test_a_failed_issue_run_is_recorded_on_the_timeline_and_notified(container, make_agent, models):
    from muliao.issues.rules import FAILURE_SUMMARY_MAX

    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = _failing(400, times=1)
    issue = await container.issues.create(None, owner=alpha, title="Fix it", status=TODO, assignee=alpha)
    await drain(container)

    detail = await container.issues.get(issue.id)
    failed = detail.activities[-1]
    assert failed.kind == ActivityKind.RUN_FAILED and failed.actor_agent_id == alpha
    assert failed.body.startswith("ModelHTTPError") and "400" in failed.body
    assert len(failed.body) == FAILURE_SUMMARY_MAX and failed.body.endswith("…")
    # 状态不变、不自动重试：失败的动态不投进会话
    assert detail.issue.status == TODO
    [session] = await _issue_sessions(container)
    assert len(await _mail(container, session.id)) == 1
    [note] = await _notifications(container)
    assert note.title == f"#{issue.id} 运行失败：Fix it" and note.session_id == session.id
    assert note.body == failed.body
    assert "alpha's run in its issue session failed: ModelHTTPError" in await container.issues.render(
        issue.id
    )

    # 之后的动态照常投递、唤醒 agent
    await container.issues.comment(None, issue.id, "try again")
    await drain(container)
    assert (await container.issues.get(issue.id)).activities[-1].kind == ActivityKind.COMMENT
    assert len(await _mail(container, session.id)) == 2
    assert len(await _notifications(container)) == 1


async def test_failed_runs_outside_issue_sessions_and_retried_ones_are_not_recorded(
    container, make_agent, models
):
    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = _failing(400, times=1)
    await start(container, alpha, "hi", origin=Origin.TRIGGER)
    await drain(container)
    # 暂时性失败、重试后成功的不记
    models.models["m"] = _failing(503, times=1)
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    await drain(container)

    assert [a.kind for a in (await container.issues.get(issue.id)).activities] == [ActivityKind.CREATED]
    assert await _notifications(container) == []


async def test_a_run_failure_on_a_deleted_issue_is_ignored(container, make_agent):
    from muliao.sessions.events import RunFailed

    alpha = (await make_agent()).id
    event = RunFailed(
        session_id="s", agent_id=alpha, run_id=1, origin=Origin.ISSUE, origin_ref="999", error="Boom"
    )
    commit = await container.issues.on_run_failed(event, Delivery("m"))
    async with container.db.transaction() as s:
        await commit(s)
    assert await _notifications(container) == []


async def test_a_transient_failure_is_recorded_once_its_retries_are_used_up(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    models.models["m"] = _failing(503, times=1000)
    issue = await container.issues.create(None, owner=alpha, title="t", status=TODO, assignee=alpha)
    await drain(container)

    failed = [
        a for a in (await container.issues.get(issue.id)).activities if a.kind == ActivityKind.RUN_FAILED
    ]
    assert len(failed) == 1 and "503" in failed[0].body
    assert len(await _notifications(container)) == 1
