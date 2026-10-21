"""按发送方定位的会话（sessions.inbound.SessionKey）：同一发送方给同一个 agent 的来信进同一个会话，没有就建。"""

import pytest

from muliao.sessions.api import (
    Inbound,
    Origin,
    SessionActivity,
    SessionKey,
    WorkState,
    keyed_sessions_among,
    receive_command_in,
)
from muliao.sessions.inbound import inbound_of
from tests.test_flow import deliver, drain


def _keyed(agent_id: str, ref: str, text: str, origin: Origin = Origin.ISSUE) -> Inbound:
    return Inbound(
        origin=origin,
        origin_ref=ref,
        content=text,
        session=SessionKey(agent_id=agent_id, title=f"#{ref}"),
        awaited=False,
    )


async def _send(container, inbound: Inbound) -> None:
    async with container.db.transaction() as s:
        container.messaging.send_in(s, await receive_command_in(s, inbound))


async def _keyed_sessions(container, ref: str):
    async with container.db.transaction() as s:
        return (await keyed_sessions_among(s, Origin.ISSUE, [ref]))[ref]


async def test_mail_for_one_key_lands_in_one_session_per_agent(container, make_agent):
    alpha, beta = (await make_agent("alpha")).id, (await make_agent("beta")).id
    for inbound in (
        _keyed(alpha, "1", "a"),
        _keyed(alpha, "1", "b"),
        _keyed(beta, "1", "c"),
        _keyed(alpha, "2", "d"),
    ):
        await _send(container, inbound)
    await deliver(container)
    one = await _keyed_sessions(container, "1")
    assert set(one) == {alpha, beta} and one[alpha].title == "#1"
    assert (await _keyed_sessions(container, "2"))[alpha].id != one[alpha].id


async def test_undelivered_keyed_mail_keeps_the_session_running(container, make_agent):
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
    async with container.db.transaction() as s:
        with pytest.raises(ValueError):
            await receive_command_in(s, _keyed(alpha, "1", "a", origin=Origin.USER))


async def test_a_keyed_receive_command_round_trips(container, make_agent):
    alpha = (await make_agent()).id
    inbound = _keyed(alpha, "7", "hello")
    async with container.db.transaction() as s:
        command = await receive_command_in(s, inbound)
    assert inbound_of(command) == inbound


async def test_a_key_with_undelivered_mail_and_no_session_yet_is_running(container, make_agent):
    alpha = (await make_agent()).id
    activity = SessionActivity(container.queue)
    async with container.db.transaction() as s:
        assert await activity.keyed_work_states_in(s, Origin.ISSUE, [("1", alpha)]) == {("1", alpha): None}
    await _send(container, _keyed(alpha, "1", "a"))
    async with container.db.transaction() as s:
        states = await activity.keyed_work_states_in(s, Origin.ISSUE, [("1", alpha)])
    assert states == {("1", alpha): WorkState.RUNNING}
    assert await _keyed_sessions(container, "1") == {}
    await drain(container)
    async with container.db.transaction() as s:
        states = await activity.keyed_work_states_in(s, Origin.ISSUE, [("1", alpha)])
    assert states == {("1", alpha): WorkState.IDLE}
