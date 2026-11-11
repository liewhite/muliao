"""投递（触发器领域内部）：把一个事件按目标（triggers.targets）变成给会话领域或 issue 领域的定向命令，在触发器的事务里只读地校验。

和 sessions.api.receive_command_in 同一个模式：这里只读（解析地址、问接收的领域投不投得进），返回结果；触发器在同一事务里
记下自己的状态并 send_in 这些命令，由接收的领域在自己的事务里执行——记下触发与投递一起提交，恰好一次；提交后由 Messaging
马上执行投递。投不进（归属表第 6 行）由接收的领域判断、在这里换成触发器自己的说法 Undeliverable（Problem：地址写错、目标
归档、目标不在、不准续写），调用方逐种处理，不靠异常：默认目标投不进时 plan_in 返回它（整次不投）；内容里 `@route` 地址投不进的
连同那段记在 Plan.rejected，是整次不投还是只丢那段由触发器定（service._By）。每个地址只校验一次：resolve_in 只把文字解析成
目标（名称换成 id），投不投得进只在 routed_in 问一次。触发器自己的 agent 是否可用在调用之前查。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import named_agent_in
from muliao.issues.api import issue_exists_in, issue_recipient
from muliao.issues.events import PostIssueEvent
from muliao.platform.ids import new_id
from muliao.platform.messaging import Command
from muliao.sessions.api import (
    Accepted,
    NewConversation,
    Recipient,
    Refusal,
    Refused,
    SenderMail,
    SessionKey,
    TriggerOrigin,
    agent_refusal_in,
    receive_command_in,
)
from muliao.triggers.tables import Trigger
from muliao.triggers.targets import (
    ADDRESS_HELP,
    Continue,
    Destination,
    IssueTo,
    LatestOf,
    NewTo,
    Target,
    parse_address,
    split_routed,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sender:
    """事件的发送方，一个触发器：在会话领域里的身份（只交出自己的 id，编号格式归会话领域），它所属的 agent（默认目标、issue 时间线上的
    actor），来源的描述（issue 时间线上显示，如 "trigger slack-live"），新开会话与续写的会话新建时的标题。"""

    origin: TriggerOrigin
    agent_id: str
    source: str
    title: str
    latest_title: str

    @classmethod
    def of(cls, trigger: Trigger, title: str) -> Sender:
        """触发器作为发送方；title 是这次新开的会话的标题（续写的会话新建时用触发器名）。"""
        return cls(
            origin=TriggerOrigin(trigger_id=trigger.id),
            agent_id=trigger.agent_id,
            source=f"trigger {trigger.name}",
            title=title,
            latest_title=trigger.name,
        )


class Problem(StrEnum):
    """一次投递投不进的原因（触发器的说法：会话领域的 Refusal 加上地址写错）。"""

    # 地址解析不了：写法不对，或没有这个名字的 agent
    BAD_ADDRESS = "bad_address"
    # 目标 agent 已归档
    ARCHIVED = "archived"
    # 目标 agent、会话或 issue 不在了（已删除、正在删除）
    GONE = "gone"
    # 目标会话不是这个触发器开的，不准续写
    NOT_YOURS = "not_yours"


@dataclass(frozen=True)
class Undeliverable:
    """投不进：原因与给人看的说明。"""

    problem: Problem
    detail: str


@dataclass(frozen=True)
class Routed:
    """一次投递：发送方要在自己的事务里 send_in 的命令；之后问它忙不忙的句柄（会话领域定形状，触发器原样存下）；交给的会话
    （投进 issue 的、或要续写的会话还没建出来时为 None）。"""

    command: Command
    recipient: Recipient
    session_id: str | None


def _problem_of(reason: Refusal) -> Problem:
    match reason:
        case Refusal.ARCHIVED:
            return Problem.ARCHIVED
        case Refusal.GONE:
            return Problem.GONE
        case Refusal.NOT_YOURS:
            return Problem.NOT_YOURS
        case other:
            assert_never(other)


async def resolve_in(s: AsyncSession, address: str, sender_agent: str) -> Target | Undeliverable:
    """文字地址解析成目标（agent 名称换成 id），写错时 Undeliverable(BAD_ADDRESS)。只读，只解析：目标投不投得进由 routed_in
    问一次。发送方自己的 agent 存为 None：`agent:<自己>` 与 `new` 是同一个目标（同一次触发里合并成一次投递）。"""
    parsed = parse_address(address)
    if parsed is None:
        return Undeliverable(Problem.BAD_ADDRESS, f"invalid route {address!r}; use one of: {ADDRESS_HELP}")
    if parsed.to == "issue":
        assert parsed.issue_id is not None
        return IssueTo(issue_id=parsed.issue_id)
    agent_id = None
    if parsed.agent is not None:
        agent = await named_agent_in(s, parsed.agent)
        if agent is None:
            return Undeliverable(
                Problem.BAD_ADDRESS, f"invalid route {address!r}: no agent named {parsed.agent!r}"
            )
        agent_id = None if agent.id == sender_agent else agent.id
    return NewTo(agent_id=agent_id) if parsed.to == "new" else LatestOf(agent_id=agent_id)


def _undeliverable(refused: Refused) -> Undeliverable:
    return Undeliverable(_problem_of(refused.reason), f"route target: {refused.detail}")


async def _issue_problem_in(s: AsyncSession, issue_id: int) -> Undeliverable | None:
    if await issue_exists_in(s, issue_id):
        return None
    return Undeliverable(Problem.GONE, f"route target issue #{issue_id} not found")


async def check_target_in(s: AsyncSession, target: Target) -> Undeliverable | None:
    """存成默认目标之前（创建、修改触发器）确认它现在投得进：别人的 agent 能运行、issue 存在。发送方自己的 agent 由发送方查。
    只是提前报错给配置的人；投递时 routed_in 照样再问接收的领域。"""
    match target:
        case NewTo(agent_id=None) | LatestOf(agent_id=None):
            return None
        case NewTo(agent_id=str(agent_id)) | LatestOf(agent_id=str(agent_id)):
            refused = await agent_refusal_in(s, agent_id)
            return None if refused is None else _undeliverable(refused)
        case IssueTo(issue_id=issue_id):
            return await _issue_problem_in(s, issue_id)
        case other:
            assert_never(other)


@dataclass(frozen=True)
class Rejected:
    """内容里一个投不进的地址（解析不了，或目标不接受），和投给它的内容。"""

    address: str
    body: str
    problem: Undeliverable


@dataclass(frozen=True)
class Plan:
    """一个事件的投递：要 send_in 的各次投递（默认目标在前，其余按目标第一次出现的顺序；显式地址落到默认目标的段跟着默认目标走），和内容里被拒的地址。"""

    routed: list[Routed]
    rejected: list[Rejected]


async def plan_in(
    s: AsyncSession,
    sender: Sender,
    payload: str | None,
    default: Destination,
    compose: Callable[[str | None], str],
) -> Plan | Undeliverable:
    """事件内容按 `@route` 行分到各目标（targets.split_routed），同一目标的各段按出现顺序合并，每个目标一次投递，投递的
    内容是 compose(合并后的内容)（发送方在前面加上自己的说明）。没有内容（payload 为 None，如定时触发）时是默认目标的一次
    投递 compose(None)；各段都是空的时 routed 为空，没有内容的段不投。只读。

    默认目标投不进时返回它的 Undeliverable（那是发送方配置的问题，整次不投）；和默认目标合并的段跟着默认目标走。默认目标
    这次没有内容要投（内容一开头就是 `@route` 行）也照样校验，和内容里的地址一样：带了目标就一定校验。内容里的地址每个都
    解析（后面有没有内容都一样），投不进的连同那段记在 rejected，要不要整次拒绝由发送方定。"""
    if payload is None:
        match only := await _routed_logged_in(s, sender, default, compose(None)):
            case Routed():
                return Plan(routed=[only], rejected=[])
            case Undeliverable():
                return only
            case other:
                assert_never(other)
    # 每个目标：合并的内容、给它定目标的第一个地址（来自默认目标就是 None：投不进去时是默认目标的问题，不是内容的）。
    # 默认目标先占第一项：不管有没有内容落到它、内容里的地址会不会解析成同一个目标，它都在下面校验一次
    planned: dict[Destination, tuple[list[str], str | None]] = {default: ([], None)}
    rejected: list[Rejected] = []
    for address, body in split_routed(payload):
        target: Destination
        if address is None:
            target = default
        else:
            match resolved := await resolve_in(s, address, sender.agent_id):
                case Undeliverable():
                    rejected.append(Rejected(address=address, body=body, problem=resolved))
                    continue
                case _:
                    target = resolved
        if body.strip():
            bodies, first = planned.get(target, ([], address))
            planned[target] = ([*bodies, body], None if address is None else first)
    routed: list[Routed] = []
    for target, (bodies, address) in planned.items():
        if not bodies:
            # 只有没内容的默认目标会这样：只校验、命令丢弃
            match checked := await routed_in(s, sender, target, compose("")):
                case Undeliverable():
                    return checked
                case Routed():
                    continue
                case other:
                    assert_never(other)
        body = "\n".join(bodies)
        match result := await _routed_logged_in(s, sender, target, compose(body)):
            case Routed():
                routed.append(result)
            case Undeliverable() if address is None:
                return result
            case Undeliverable():
                # 只读校验不留下任何写入，记下这段即可
                rejected.append(Rejected(address=address, body=body, problem=result))
            case other:
                assert_never(other)
    return Plan(routed=routed, rejected=rejected)


async def _routed_logged_in(
    s: AsyncSession, sender: Sender, target: Destination, content: str
) -> Routed | Undeliverable:
    """routed_in，投得进时记日志（只校验、丢掉命令的那次不记）。"""
    result = await routed_in(s, sender, target, content)
    if isinstance(result, Routed):
        log.info(
            "event routed",
            extra={"source": sender.source, "target": target.model_dump(), "session_id": result.session_id},
        )
    return result


async def routed_in(
    s: AsyncSession, sender: Sender, target: Destination, content: str
) -> Routed | Undeliverable:
    """把 content 投到目标：投得进是要 send_in 的命令与句柄（Routed），投不进是原因（Undeliverable，由接收的领域判断）。
    只读。发送方已确认自己可用，所以投不进都是目标的问题（存下的目标后来被删除、归档，会话不接受这个发送方续写）。"""
    match target:
        case Continue(session_id=session_id):
            session: str | NewConversation | SessionKey = session_id
        case NewTo(agent_id=agent_id):
            session = NewConversation(id=new_id(), agent_id=agent_id or sender.agent_id, title=sender.title)
        case LatestOf(agent_id=agent_id):
            session = SessionKey(agent_id=agent_id or sender.agent_id, title=sender.latest_title)
        case IssueTo(issue_id=issue_id):
            if (problem := await _issue_problem_in(s, issue_id)) is not None:
                return problem
            command = PostIssueEvent(
                issue_id=issue_id, agent_id=sender.agent_id, source=sender.source, body=content
            )
            return Routed(command=command, recipient=issue_recipient(issue_id), session_id=None)
        case other:
            assert_never(other)
    mail = SenderMail(sender=sender.origin, content=content, session=session)
    match await receive_command_in(s, mail):
        case Accepted(command=command, recipient=recipient, session_id=session_id):
            return Routed(command=command, recipient=recipient, session_id=session_id)
        case Refused() as refused:
            return _undeliverable(refused)
        case other:
            assert_never(other)
