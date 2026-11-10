"""投递：把一个事件按目标（routing.targets）变成给会话领域或 issue 领域的定向命令，在发送方的事务里只读地校验。

和 sessions.api.receive_command_in 同一个模式：这里只读（解析地址、确认 agent 与 issue 存在、校验来信），返回命令；
发送方（如触发器）在同一事务里记下自己的状态并 send_in 这些命令，由接收的领域在自己的事务里执行——记下触发与投递
一起提交，恰好一次；提交后发送方让投递任务马上执行（Worker.run_now）。目标不合法（地址写错、agent 或 issue 不存在、
agent 已归档、会话不接受这个发送方续写）时报领域错误，发送方的事务回滚，整次都不投。发送方自己（如触发器所属的
agent）是否可用由发送方在调用之前查，这里出的任何领域错误都是目标的问题，发送方不按异常类型分辨。
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import named_agent_in, runnable_agent_in
from muliao.errors import DomainError, InvalidInput, NotFound
from muliao.issues.api import check_issue_in
from muliao.issues.events import PostIssueEvent
from muliao.platform.ids import new_id
from muliao.platform.messaging import Command
from muliao.routing.targets import (
    Continue,
    Destination,
    IssueTo,
    LatestOf,
    NewTo,
    Target,
    parse_address,
    split_routed,
)
from muliao.sessions.api import (
    NewConversation,
    Origin,
    SenderMail,
    SessionKey,
    keyed_session_in,
    receive_command_in,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sender:
    """事件的发送方：会话领域里的身份（origin, origin_ref），它所属的 agent（默认目标、issue 时间线上的 actor），来源的
    描述（issue 时间线上显示，如 "trigger slack-live"），新开会话与续写的会话新建时的标题。"""

    origin: Origin
    origin_ref: str
    agent_id: str
    source: str
    title: str
    latest_title: str


@dataclass(frozen=True)
class Routed:
    """一次投递：发送方要在自己的事务里 send_in 的命令，交给的会话（投进 issue 的、或要续写的会话还没建出来时为 None）。"""

    command: Command
    session_id: str | None


async def resolve_in(s: AsyncSession, address: str, sender_agent: str) -> Target:
    """文字地址解析成目标（agent 名称换成 id 并确认它能运行，确认 issue 存在）；不合法时 InvalidInput。只读。发送方自己的
    agent 存为 None：`agent:<自己>` 与 `new` 是同一个目标（同一次触发里合并成一次投递），它是否可用由发送方查。"""
    parsed = parse_address(address)
    try:
        if parsed.to == "issue":
            assert parsed.issue_id is not None
            await check_issue_in(s, parsed.issue_id)
            return IssueTo(issue_id=parsed.issue_id)
        agent_id = None if parsed.agent is None else (await named_agent_in(s, parsed.agent)).id
        if agent_id == sender_agent:
            agent_id = None
        elif agent_id is not None:
            await runnable_agent_in(s, agent_id)
    except DomainError as e:
        raise InvalidInput(f"invalid route {address!r}: {e}") from e
    return NewTo(agent_id=agent_id) if parsed.to == "new" else LatestOf(agent_id=agent_id)


async def plan_in(
    s: AsyncSession, payload: str | None, default: Destination, sender_agent: str
) -> list[tuple[Destination, str | None]]:
    """事件内容按 `@route` 行分到各目标（targets.split_routed）：[(目标, 这段内容)]，同一目标的各段按出现顺序合并。
    每个地址都解析（写错的地址整次报错，后面有没有内容都一样），没有内容的段不投。没有内容（payload 为 None，如定时
    触发）时是默认目标的一次投递；有内容但各段都是空的时为空列表。只读。"""
    if payload is None:
        return [(default, None)]
    planned: dict[Destination, list[str]] = {}
    for address, body in split_routed(payload):
        target = default if address is None else await resolve_in(s, address, sender_agent)
        if body.strip():
            planned.setdefault(target, []).append(body)
    return [(target, "\n".join(bodies)) for target, bodies in planned.items()]


async def routed_in(s: AsyncSession, sender: Sender, target: Destination, content: str) -> Routed:
    """把 content 投到目标的命令（只读地校验，不合规时当场报错，见 sessions.api.receive_command_in）。发送方已确认自己可用，
    所以这里的任何领域错误（存下的目标后来被删除、归档，会话不接受这个发送方续写）都是目标的问题，照原类型报给调用方
    （如 webhook 续写别人的会话是 Forbidden）。只有 NotFound 换成 InvalidInput：它在发送方那里会被当成发送方自己不存在
    （如 HTTP 404 的 webhook）。"""
    try:
        routed = await _routed_in(s, sender, target, content)
    except NotFound as e:
        raise InvalidInput(f"route target not found or gone: {e}") from e
    log.info(
        "event routed",
        extra={"source": sender.source, "target": target.model_dump(), "session_id": routed.session_id},
    )
    return routed


async def _routed_in(s: AsyncSession, sender: Sender, target: Destination, content: str) -> Routed:
    match target:
        case Continue(session_id=session_id):
            mail = SenderMail(
                origin=sender.origin, origin_ref=sender.origin_ref, content=content, session=session_id
            )
            return Routed(command=await receive_command_in(s, mail), session_id=session_id)
        case NewTo(agent_id=agent_id):
            session_id = new_id()
            mail = SenderMail(
                origin=sender.origin,
                origin_ref=sender.origin_ref,
                content=content,
                session=NewConversation(
                    id=session_id, agent_id=agent_id or sender.agent_id, title=sender.title
                ),
            )
            return Routed(command=await receive_command_in(s, mail), session_id=session_id)
        case LatestOf(agent_id=agent_id):
            agent = agent_id or sender.agent_id
            mail = SenderMail(
                origin=sender.origin,
                origin_ref=sender.origin_ref,
                content=content,
                session=SessionKey(agent_id=agent, title=sender.latest_title),
            )
            command = await receive_command_in(s, mail)
            existing = await keyed_session_in(s, sender.origin, sender.origin_ref, agent)
            return Routed(command=command, session_id=None if existing is None else existing.id)
        case IssueTo(issue_id=issue_id):
            await check_issue_in(s, issue_id)
            command = PostIssueEvent(
                issue_id=issue_id, agent_id=sender.agent_id, source=sender.source, body=content
            )
            return Routed(command=command, session_id=None)
