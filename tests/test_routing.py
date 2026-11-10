"""事件投递（routing）：触发器的默认投递目标与事件文本里的 `@route` 行，把事件投到新会话、别的 agent、最近的会话或 issue。"""

import pytest
from sqlalchemy import select

from muliao.agents.api import USER, AgentArchived
from muliao.errors import InvalidInput
from muliao.issues.api import ActivityKind, IssueStatus
from muliao.routing.targets import parse_address, routed_text, split_routed
from muliao.sessions.api import Origin
from muliao.sessions.tables import Mail, Session
from muliao.triggers.api import route_address
from tests.test_flow import deliver


def test_text_without_route_lines_is_one_part_for_the_default_target_unchanged():
    text = "line 1\n\nline 2\n"
    assert split_routed(text) == [(None, text)]


def test_route_lines_split_the_text_in_order_and_keep_empty_parts_so_their_address_is_checked():
    text = "head\n@route agent:beta\nb1\n@route issue:7\ni1\n@route agent:beta\nb2\n@route session\n\n"
    assert split_routed(text) == [
        (None, "head"),
        ("agent:beta", "b1"),
        ("issue:7", "i1"),
        ("agent:beta", "b2"),
        ("session", ""),
    ]


def test_routed_text_puts_the_default_lines_first_and_splits_back_the_same():
    sections = {"issue:7": ["i1", "i2"], None: ["d1"], "agent:beta": []}
    text = routed_text(sections)
    assert text == "d1\n@route issue:7\ni1\ni2\n@route agent:beta"
    assert split_routed(text) == [(None, "d1"), ("issue:7", "i1\ni2"), ("agent:beta", "")]
    assert routed_text({None: ["a", "b"]}) == "a\nb"


def test_only_route_lines_at_the_start_count_so_scripts_can_escape_outside_text():
    text = "msg\n @route agent:muliao\n\t@route x\n"
    assert split_routed(text) == [(None, text)]


@pytest.mark.parametrize(
    ("text", "to", "agent", "issue_id"),
    [
        ("new", "new", None, None),
        ("agent:beta", "new", "beta", None),
        ("session", "session", None, None),
        ("session:beta", "session", "beta", None),
        ("issue:12", "issue", None, 12),
        ("issue:#12", "issue", None, 12),
    ],
)
def test_addresses_parse(text, to, agent, issue_id):
    parsed = parse_address(text)
    assert (parsed.to, parsed.agent, parsed.issue_id) == (to, agent, issue_id)


@pytest.mark.parametrize("text", ["", "agent", "agent:", "issue:x", "slack:C123", "new:beta"])
def test_bad_addresses_are_rejected(text):
    with pytest.raises(InvalidInput, match="invalid route"):
        parse_address(text)


async def _sessions(container, origin: Origin = Origin.TRIGGER) -> list[Session]:
    async with container.db.transaction() as s:
        query = select(Session).where(Session.origin == origin).order_by(Session.created_at)
        return list((await s.scalars(query)).all())


async def _mail(container, session_id: str) -> list[str]:
    async with container.db.transaction() as s:
        rows = await s.scalars(select(Mail).where(Mail.session_id == session_id).order_by(Mail.id))
        return [m.content for m in rows]


async def _webhook(container, agent_id: str, **kw):
    return await container.triggers.create(
        name="feed", agent_id=agent_id, kind="webhook", prompt="Handle it.", config={}, **kw
    )


async def test_route_lines_in_an_event_send_parts_to_other_agents_in_one_firing(container, make_agent):
    alpha, beta = await make_agent("alpha"), await make_agent("beta")
    trigger = await _webhook(container, alpha.id)

    fired = await container.triggers.fire(
        trigger.id, "mine\n@route agent:beta\nfor beta 1\n@route agent:beta\nfor beta 2"
    )
    await deliver(container)

    first, second = await _sessions(container)
    assert fired is not None and fired.sessions == (first.id, second.id)
    assert (first.agent_id, second.agent_id) == (alpha.id, beta.id)
    assert await _mail(container, first.id) == ["Handle it.\n\n<event>\nmine\n</event>"]
    assert await _mail(container, second.id) == ["Handle it.\n\n<event>\nfor beta 1\nfor beta 2\n</event>"]
    # 都是这个触发器发起的：它的会话列表查得到
    assert {s.origin_ref for s in (first, second)} == {str(trigger.id)}


async def test_naming_the_triggers_own_agent_is_the_same_target_as_new(container, make_agent):
    alpha = await make_agent("alpha")
    trigger = await _webhook(container, alpha.id, route="agent:alpha")
    assert trigger.route is None

    fired = await container.triggers.fire(trigger.id, "a\n@route new\nb\n@route agent:alpha\nc")
    await deliver(container)

    [session] = await _sessions(container)
    assert fired is not None and fired.sessions == (session.id,)
    assert await _mail(container, session.id) == ["Handle it.\n\n<event>\na\nb\nc\n</event>"]


async def test_last_session_is_kept_when_a_firing_only_went_to_an_issue(container, make_agent):
    alpha = await make_agent("alpha")
    issue = await container.issues.create(USER, owner=alpha.id, title="t")
    trigger = await _webhook(container, alpha.id)
    first = await container.triggers.fire(trigger.id, "x")
    assert first is not None

    await container.triggers.fire(trigger.id, f"@route issue:{issue.id}\ny")
    assert (await container.triggers.get_by_name("feed")).last_session_id == first.sessions[0]


async def test_session_route_continues_the_latest_conversation_of_the_trigger(container, make_agent):
    alpha = await make_agent("alpha")
    trigger = await _webhook(container, alpha.id, route="session")
    assert route_address(trigger, await container.agents.names()) == "session"

    first = await container.triggers.fire(trigger.id, "one")
    await deliver(container)
    second = await container.triggers.fire(trigger.id, "two")
    await deliver(container)

    [session] = await _sessions(container)
    assert first is not None and first.sessions == (None,)  # 第一次时会话还没建出来
    assert second is not None and second.sessions == (session.id,)
    assert session.title == "feed"
    assert [m.split("<event>\n")[1] for m in await _mail(container, session.id)] == [
        "one\n</event>",
        "two\n</event>",
    ]


async def test_issue_route_records_an_event_and_delivers_it_like_a_user_comment(container, make_agent):
    alpha, beta = await make_agent("alpha"), await make_agent("beta")
    issue = await container.issues.create(USER, owner=alpha.id, title="watch", assignee=beta.id)
    trigger = await _webhook(container, alpha.id, route=f"issue:{issue.id}")

    # backlog：没人接手，像用户评论一样投给 owner
    fired = await container.triggers.fire(trigger.id, "price moved")
    await deliver(container)
    assert fired is not None and fired.sessions == (None,)
    assert await _sessions(container) == []  # 没开触发器自己的会话
    [owner_session] = await _sessions(container, Origin.ISSUE)
    assert owner_session.agent_id == alpha.id
    [letter] = await _mail(container, owner_session.id)
    assert "event from trigger feed (alpha):\n  Handle it." in letter and "price moved" in letter

    # in_progress：只投给被指派的 beta
    await container.issues.update(USER, issue.id, status=IssueStatus.IN_PROGRESS)
    await deliver(container)
    await container.triggers.fire(trigger.id, "again")
    await deliver(container)
    sessions = {s.agent_id: s for s in await _sessions(container, Origin.ISSUE)}
    assert "again" in (await _mail(container, sessions[beta.id].id))[-1]
    assert len(await _mail(container, owner_session.id)) == 1

    detail = await container.issues.get(issue.id)
    events = [a for a in detail.activities if a.kind == ActivityKind.EVENT]
    assert [(a.actor, a.change) for a in events] == [(alpha.id, {"from": None, "to": "trigger feed"})] * 2
    assert "<event>\nagain\n</event>" in (events[1].body or "")


async def test_a_bad_route_rejects_a_firing_someone_waits_for_and_drops_just_its_part_when_activated(
    container, make_agent
):
    """#569：调用方在等的（webhook、trigger_run）整次报错；激活机制只丢那一段、其余照投，触发器保持启用，记在 route_error，
    每种坏地址只给触发器的 agent 开一个告警会话（好坏批次交替不重复告警）；改了配置或重新启用时清除。"""
    alpha = await make_agent("alpha")
    trigger = await _webhook(container, alpha.id)
    bad = "ok\n@route agent:nobody\nlost"

    with pytest.raises(InvalidInput, match="no agent named 'nobody'"):
        await container.triggers.fire(trigger.id, bad)
    assert await _sessions(container) == []  # 整次都不投
    assert (await container.triggers.get_by_name("feed")).route_error is None

    # 投信与告警都在返回时已投进会话（提交后马上执行）
    await container.triggers.fire_activated(trigger.id, bad)
    after = await container.triggers.get_by_name("feed")
    assert after.enabled and after.disabled_reason is None
    assert after.route_error is not None and "@route agent:nobody" in after.route_error
    [alert] = [x for x in await _sessions(container) if "投递地址写错" in x.title]
    [event] = [x for x in await _sessions(container) if x.id != alert.id]
    assert await _mail(container, event.id) == ["Handle it.\n\n<event>\nok\n</event>"]
    assert alert.agent_id == alpha.id
    [letter] = await _mail(container, alert.id)
    assert "`@route agent:nobody`" in letter and "lost" in letter

    # 同一种错：照样丢段、不再告警；各段都丢了就不触发；中间有好的批次也一样
    await container.triggers.fire_activated(trigger.id, "@route agent:nobody\nlost again")
    await container.triggers.fire_activated(trigger.id, "fine")
    await container.triggers.fire_activated(trigger.id, bad)
    await deliver(container)
    assert len(await _sessions(container)) == 4  # 另两次事件

    # 新的一种坏地址告警一次，只说新的，route_error 两种都记着
    await container.triggers.fire_activated(trigger.id, "@route issue:999\nnope")
    await deliver(container)
    alerts = [x for x in await _sessions(container) if "投递地址写错" in x.title]
    assert len(alerts) == 2
    newest = [m for x in alerts if x.id != alert.id for m in await _mail(container, x.id)]
    assert "issue:999" in newest[0] and "nobody" not in newest[0]
    error = (await container.triggers.get_by_name("feed")).route_error or ""
    assert "nobody" in error and "issue:999" in error

    # 重新启用（改配置同理）清除，再出错重新告警
    await container.triggers.update(trigger.id, enabled=False)
    await container.triggers.update(trigger.id, enabled=True)
    assert (await container.triggers.get_by_name("feed")).route_error is None
    await container.triggers.fire_activated(trigger.id, bad)
    await deliver(container)
    assert len([x for x in await _sessions(container) if "投递地址写错" in x.title]) == 3


async def test_a_part_whose_target_refuses_it_is_dropped_but_a_bad_default_target_still_disables(
    container, make_agent, monkeypatch
):
    """地址解析得了、投递时目标不接受：内容里的地址只丢那段；和默认目标合并的段或默认目标本身不接受仍是触发器配置的问题。"""
    from muliao.errors import Forbidden
    from muliao.routing.api import NewTo
    from muliao.triggers import service

    alpha, beta = await make_agent("alpha"), await make_agent("beta")
    real = service.routed_in

    async def refusing(s, sender, target, content):
        if target == NewTo(agent_id=beta.id):
            raise Forbidden("beta refuses")
        return await real(s, sender, target, content)

    monkeypatch.setattr(service, "routed_in", refusing)
    trigger = await _webhook(container, alpha.id)
    await container.triggers.fire_activated(trigger.id, "mine\n@route agent:beta\nfor beta")
    await deliver(container)
    after = await container.triggers.get_by_name("feed")
    assert after.enabled and "beta refuses" in (after.route_error or "")

    stored = await container.triggers.update(trigger.id, route="agent:beta")
    await container.triggers.fire_activated(stored.id, "x\n@route agent:beta\ny")
    after = await container.triggers.get_by_name("feed")
    assert not after.enabled and "beta refuses" in (after.disabled_reason or "")


async def test_the_default_route_is_checked_when_set_and_again_when_its_target_is_gone(container, make_agent):
    alpha = await make_agent("alpha")
    await make_agent("beta")
    with pytest.raises(InvalidInput, match="issue #99 not found"):
        await _webhook(container, alpha.id, route="issue:99")
    with pytest.raises(InvalidInput, match="invalid route"):
        await _webhook(container, alpha.id, route="slack:C1")

    issue = await container.issues.create(USER, owner=alpha.id, title="t")
    trigger = await _webhook(container, alpha.id, route=f"issue:{issue.id}")
    trigger = await container.triggers.update(trigger.id, route="session:beta")
    assert route_address(trigger, await container.agents.names()) == "session:beta"
    trigger = await container.triggers.update(trigger.id, route="new")
    assert trigger.route is None

    trigger = await container.triggers.update(trigger.id, route=f"issue:{issue.id}")
    await container.issues.delete(issue.id)
    with pytest.raises(InvalidInput, match="route target not found or gone"):
        await container.triggers.fire(trigger.id, "x")


async def test_an_address_without_content_is_still_checked(container, make_agent):
    alpha = await make_agent("alpha")
    trigger = await _webhook(container, alpha.id)
    with pytest.raises(InvalidInput, match="no agent named 'nobody'"):
        await container.triggers.fire(trigger.id, "x\n@route agent:nobody\n")
    assert await _sessions(container) == []


async def test_a_route_to_an_archived_agent_is_a_bad_target_not_the_trigger_being_archived(
    container, make_agent
):
    """目标 agent 归档是目标不合法，不当成触发器自己的 agent 归档而静默跳过：默认目标是它时整次不投、激活的触发停用并记原因；
    内容里的地址是它时激活的触发只丢那段（#569）。"""
    alpha, beta = await make_agent("alpha"), await make_agent("beta")
    trigger = await container.triggers.create(
        name="later", agent_id=alpha.id, kind="once", prompt="p", config={"at": "2099-01-01T00:00"}
    )
    stored = await _webhook(container, alpha.id, route="agent:beta")
    await container.agent_lifecycle.set_archived(beta.id, True)

    with pytest.raises(AgentArchived, match="beta.*archived"):
        await container.triggers.fire(stored.id, "x")
    with pytest.raises(InvalidInput, match="invalid route 'session:beta'.*archived"):
        await container.triggers.update(stored.id, route="session:beta")

    await container.triggers.fire_activated(stored.id, "x")
    after = await container.triggers.get_by_name("feed")
    assert not after.enabled and "archived" in (after.disabled_reason or "")

    await container.triggers.fire_activated(trigger.id, "mine\n@route agent:beta\nfor beta")
    await deliver(container)
    after = await container.triggers.get_by_name("later")
    assert after.enabled and "archived" in (after.route_error or "")
    assert {s.agent_id for s in await _sessions(container)} == {alpha.id}  # 自己那段与告警
