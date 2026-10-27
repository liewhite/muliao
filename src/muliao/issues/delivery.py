"""issue 动态的投递：按 rules.recipient 判定这次的动态投给谁，在调用方的事务里发出定向命令 Receive，提交后马上
执行（run_now）。信投进这个 agent 在这个 issue 上唯一的会话：会话领域按发送方（Origin.ISSUE，origin_ref 为
issue id）与 agent 定位，没有就新建（sessions.api.SessionKey）；这里不存会话 id。agent 把 issue 交给用户时同一事务
发通知（Notify）。"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentArchived, agent_names_in
from muliao.issues.graph import Links, links_in, waiting_on
from muliao.issues.render import delivery_message, issue_view
from muliao.issues.rules import USER, Snapshot, recipient
from muliao.issues.tables import ActivityKind, Issue, IssueActivity
from muliao.notifications.events import Notify
from muliao.platform.messaging import Messaging
from muliao.platform.tables import Task
from muliao.sessions.api import Origin, SenderMail, SessionKey, keyed_sessions_among, receive_command_in

log = logging.getLogger(__name__)


def snapshot(issue: Issue, links: Links) -> Snapshot:
    """投递与停滞判定看的那部分状态。"""
    return Snapshot(
        status=issue.status,
        assignee=issue.assignee,
        owner=issue.owner_agent_id,
        waiting_on=waiting_on(links.depends_on),
    )


class IssueDelivery:
    def __init__(self, messaging: Messaging, tz: ZoneInfo, run_now: Callable[[int], Awaitable[None]]):
        self._messaging = messaging
        self._tz = tz
        # 马上执行一个已提交的投递任务（Worker.run_now）
        self._run_now = run_now

    async def changed_in(
        self,
        s: AsyncSession,
        assignee_before: str | None,
        issue: Issue,
        actor: str | None,
        session_id: str | None,
        happened: Sequence[IssueActivity],
    ) -> Task | None:
        """issue 的字段变了（或刚建）：agent 把它交给用户时推送通知，再投递这些动态。"""
        if actor is not None and issue.assignee == USER and assignee_before != USER:
            # agent 把 issue 交给用户：推送通知，点开是 issue（在那里通过或打回）；也带上它交出 issue 的那个会话
            # （打开会话即已读，会话删除时通知随之删除）
            self._messaging.send_in(
                s,
                Notify(
                    agent_id=actor,
                    title=f"#{issue.id} 指派给你：{issue.title}",
                    body=f"状态 {issue.status}",
                    session_id=session_id,
                    issue_id=issue.id,
                ),
            )
        return await self.deliver_in(s, issue, actor, session_id, happened)

    async def deliver_in(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str | None,
        session_id: str | None,
        happened: Sequence[IssueActivity],
    ) -> Task | None:
        """在调用方的事务里把这次的动态投给该收到的 agent（rules.recipient），进它在这个 issue 上唯一的会话。返回要
        提交后马上执行的投递（run）。"""
        links = (await links_in(s, [issue.id]))[issue.id]
        user_comment = actor is None and any(a.kind == ActivityKind.COMMENT for a in happened)
        agent = recipient(
            snapshot(issue, links),
            user_comment=user_comment,
            from_its_session=await _from_its_session_in(s, issue, actor, session_id),
        )
        if agent is None:
            return None
        # 信里只有摘要和这次的动态：只渲染 happened，不读整条时间线
        letter = issue_view(issue, happened, links, await agent_names_in(s), self._tz)
        inbound = SenderMail(
            origin=Origin.ISSUE,
            origin_ref=str(issue.id),
            content=delivery_message(letter),
            session=SessionKey(agent_id=agent, title=f"#{issue.id} {issue.title}"),
        )
        try:
            command = await receive_command_in(s, inbound)
        except AgentArchived:
            # 已有 issue 的指派人或 owner 后来被归档：动态照常记进时间线，不投递（否则用户在这类 issue 上的评论、改动
            # 都会报错）；取消归档后由 agent 自己 issue_show 看
            log.info(
                "issue activity not delivered: agent archived", extra={"issue_id": issue.id, "agent": agent}
            )
            return None
        delivery = self._messaging.send_in(s, command)
        log.info("issue activity delivered", extra={"issue_id": issue.id, "agent": agent, "actor": actor})
        return delivery

    async def run(self, *wake: Task | None) -> None:
        """事务提交之后马上执行这些投递。"""
        for task in wake:
            if task is not None:
                await self._run_now(task.id)


async def _from_its_session_in(
    s: AsyncSession, issue: Issue, actor: str | None, session_id: str | None
) -> str | None:
    """动作是不是 agent 在它自己这个 issue 会话里做的：是则返回它（这次动态不投回那里）。"""
    if actor is None or session_id is None:
        return None
    ref = str(issue.id)
    its_session = (await keyed_sessions_among(s, Origin.ISSUE, [ref]))[ref].get(actor)
    return actor if its_session is not None and its_session.id == session_id else None
