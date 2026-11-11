"""按发送方定位的会话（sessions.inbound.SessionKey）：同一发送方给同一个 agent 的来信进同一个会话，没有就建。"""

import pytest

from muliao.sessions.api import (
    Accepted,
    DelegationOrigin,
    IssueOrigin,
    Origin,
    SenderInbox,
    SenderMail,
    SessionActivity,
    SessionKey,
    TriggerOrigin,
    UserOrigin,
    WorkState,
    keyed_sessions_among,
)
from tests.test_flow import drain

ONE = IssueOrigin(issue_id=1)


def _keyed(agent_id: str, ref: str, text: str) -> SenderMail:
    return SenderMail(
        sender=IssueOrigin(issue_id=int(ref)),
        content=text,
        session=SessionKey(agent_id=agent_id, title=f"#{ref}"),
    )


async def _send(container, inbound: SenderMail) -> None:
    async with container.db.transaction() as s:
        accepted = await SenderInbox(container.queue).receive_in(s, inbound)
        assert isinstance(accepted, Accepted)


async def _keyed_sessions(container, ref: str):
    async with container.db.transaction() as s:
        sender = IssueOrigin(issue_id=int(ref))
        return (await keyed_sessions_among(s, [sender]))[sender]


async def test_mail_for_one_key_lands_in_one_session_per_agent(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    for inbound in (
        _keyed(alpha, "1", "a"),
        _keyed(alpha, "1", "b"),
        _keyed(beta, "1", "c"),
        _keyed(alpha, "2", "d"),
    ):
        await _send(container, inbound)
    one = await _keyed_sessions(container, "1")
    assert set(one) == {alpha, beta} and one[alpha].title == "#1"
    assert (await _keyed_sessions(container, "2"))[alpha].id != one[alpha].id


async def test_new_keyed_mail_makes_the_session_running(container, make_agent):
    alpha = (await make_agent()).id
    await _send(container, _keyed(alpha, "1", "a"))
    await drain(container)
    session = (await _keyed_sessions(container, "1"))[alpha]
    async with container.db.transaction() as s:
        assert await SessionActivity(container.queue).work_state_in(s, session.id) == WorkState.IDLE
    await _send(container, _keyed(alpha, "1", "b"))
    async with container.db.transaction() as s:
        assert await SessionActivity(container.queue).work_state_in(s, session.id) == WorkState.RUNNING


async def test_a_key_needs_a_sender_identity(container, make_agent):
    alpha = (await make_agent()).id
    # 只有别的领域（有身份的发送方）发 SenderMail：用户、委派的来信在构造时就拒绝
    for sender in (UserOrigin(), DelegationOrigin()):
        with pytest.raises(ValueError):
            SenderMail(sender=sender, content="a", session=SessionKey(agent_id=alpha, title="t"))


async def test_a_key_without_a_session_has_no_state(container, make_agent):
    alpha = (await make_agent()).id
    activity = SessionActivity(container.queue)
    async with container.db.transaction() as s:
        assert await activity.keyed_work_states_in(s, [(ONE, alpha)]) == {(ONE, alpha): None}
    await _send(container, _keyed(alpha, "1", "a"))
    async with container.db.transaction() as s:
        assert await activity.keyed_work_states_in(s, [(ONE, alpha)]) == {(ONE, alpha): WorkState.RUNNING}


def test_every_origin_has_its_rules():
    from muliao.sessions.origins import RULES

    assert set(RULES) == set(Origin)


async def test_senders_of_different_kinds_with_the_same_id_have_their_own_sessions(container, make_agent):
    alpha = (await make_agent()).id
    issue, trigger = IssueOrigin(issue_id=5), TriggerOrigin(trigger_id=5)
    for sender in (issue, trigger):
        await _send(
            container, SenderMail(sender=sender, content="x", session=SessionKey(agent_id=alpha, title="t"))
        )
    async with container.db.transaction() as s:
        found = await keyed_sessions_among(s, [issue, trigger, TriggerOrigin(trigger_id=7)])
    assert found[issue][alpha].id != found[trigger][alpha].id
    assert found[issue][alpha].origin == issue and found[TriggerOrigin(trigger_id=7)] == {}
