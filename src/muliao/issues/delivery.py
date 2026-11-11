"""issue 动态的投递：按 rules.recipient 判定这次的动态投给谁，在调用方的事务里发出定向命令 Receive（提交后由
Messaging 马上执行）。信投进这个 agent 在这个 issue 上唯一的会话：身份见 issues.conversation，没有就新建；这里不存会话 id。这次的动态里 agent 把 issue 交给了
用户（rules.handed_to_user）时同一事务发通知（Notify）。"""

import logging
from collections.abc import Sequence
from typing import assert_never
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import USER, agent_names_in
from muliao.issues import conversation
from muliao.issues.activity import Activity
from muliao.issues.graph import links_in, snapshot
from muliao.issues.render import delivery_message, issue_view
from muliao.issues.rules import agent_actor, from_outside, handed_to_user, recipient
from muliao.issues.tables import Issue
from muliao.notifications.events import Notify
from muliao.platform.messaging import Messaging
from muliao.sessions.api import Accepted, Refusal, Refused, receive_command_in

log = logging.getLogger(__name__)


class IssueDelivery:
    def __init__(self, messaging: Messaging, tz: ZoneInfo):
        self._messaging = messaging
        self._tz = tz

    async def post_in(
        self,
        s: AsyncSession,
        issue: Issue,
        actor: str,
        session_id: str | None,
        happened: Sequence[Activity],
        *,
        to: str | None = None,
    ) -> None:
        """只由变更单元（issues.change）结算时调用。在调用方的事务里发出这次动态带出的消息：agent 把 issue 交给用户时推送通知；把动态投给该收到的 agent
        （rules.recipient），进它在这个 issue 上唯一的会话。to：直接投给这个 agent，不按 rules.recipient 判定
        （issue_stop 的收尾消息投给被停下的 agent，不论 issue 的新状态）。"""
        what = [a.what for a in happened]
        if (agent := handed_to_user(actor, issue.assignee, what)) is not None:
            # 推送通知，点开是 issue（在那里通过或打回）；也带上它交出 issue 的那个会话（打开会话即已读，会话删除时
            # 通知随之删除）
            self._messaging.send_in(
                s,
                Notify(
                    agent_id=agent,
                    title=f"#{issue.id} 指派给你：{issue.title}",
                    body=f"状态 {issue.status}",
                    session_id=session_id,
                    issue_id=issue.id,
                ),
            )
        links = (await links_in(s, [issue.id]))[issue.id]
        if to is None:
            to = recipient(
                snapshot(issue, links),
                from_outside=from_outside(actor, what),
                from_its_session=await _from_its_session_in(s, issue, actor, session_id),
            )
        if to is None:
            return
        names = await agent_names_in(s)
        # 信里只有摘要和这次的动态：只渲染 happened，不读整条时间线
        letter = issue_view(issue, happened, links, names, self._tz)
        inbound = conversation.mail(issue.id, to, f"#{issue.id} {issue.title}", delivery_message(letter))
        match result := await receive_command_in(s, inbound):
            case Accepted(command=command):
                self._messaging.send_in(s, command)
                log.info(
                    "issue activity delivered", extra={"issue_id": issue.id, "agent": to, "actor": actor}
                )
            case Refused(reason=Refusal.ARCHIVED) if actor == USER:
                # 已有 issue 的指派人或 owner 后来被归档：动态照常记进时间线（不让用户在这类 issue 上的评论、改动报错），
                # 不投递；用户自己的改动、评论没人接要当场告诉他（只有他能取消归档或改指派）。别人的动态（agent、触发器
                # 的事件）只记日志：不然周期触发器投进这类 issue 会每个周期推一条，in_progress 的由看板的「停滞」标出
                self._messaging.send_in(
                    s,
                    Notify(
                        agent_id=to,
                        title=f"#{issue.id} 的动态没交给 {names[to]}：它已归档",
                        body="取消归档后它用 issue_show 能看到这些动态；也可以把 issue 改指派给别的 agent。",
                        session_id=None,
                        issue_id=issue.id,
                    ),
                )
                log.info("issue activity not delivered: %s", result.detail, extra={"issue_id": issue.id})
            case Refused(reason=Refusal.ARCHIVED):
                log.info("issue activity not delivered: %s", result.detail, extra={"issue_id": issue.id})
            case Refused(reason=Refusal.GONE):
                # agent 正在删除：删除流程会清理它在 issue 上的指派与 owner（on_agent_deleting），不投
                log.info("issue activity not delivered: %s", result.detail, extra={"issue_id": issue.id})
            case Refused(reason=Refusal.NOT_YOURS):
                raise RuntimeError(
                    f"issue #{issue.id}: mail to the agent's own issue session was refused as not ours: {result.detail}"
                )
            case Refused(reason=other):
                assert_never(other)


async def _from_its_session_in(
    s: AsyncSession, issue: Issue, actor: str, session_id: str | None
) -> str | None:
    """动作是不是 agent 在它自己这个 issue 会话里做的：是则返回它（这次动态不投回那里）。"""
    if agent_actor(actor) is None or session_id is None:
        return None
    its_session = await conversation.session_in(s, issue.id, actor)
    return actor if its_session is not None and its_session.id == session_id else None
