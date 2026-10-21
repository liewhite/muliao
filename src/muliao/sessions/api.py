"""会话领域的公开接口：别的领域只经这里（与 sessions.events）使用会话领域（docs/architecture.md 第 7 节、2.3）。

- 来信：发送方在自己的事务里 check_receivable_in，再发出 receive_command 构造的定向命令 Receive（sessions.events）；
- 后台任务：SessionWork.hold（在会话领域自己的事务里登记占用），hold_registered_in；结局与运行中检查经定向命令
  WorkEnded / WorkCheckIn（sessions.events）；
- 查询：session_in（SessionInfo）、SessionActivity（会话忙不忙）、AgentHistory（反思用的步数与记录）；
- 会话种类与 HTTP 层用的读模型、展示事件；停止的结果：Halted。
"""

from muliao.sessions.activity import SessionActivity, WorkState
from muliao.sessions.agent_history import AgentHistory
from muliao.sessions.halt import Halted
from muliao.sessions.holds import registered_in as hold_registered_in
from muliao.sessions.inbound import Inbound, NewSession, check_receivable_in, receive_command
from muliao.sessions.info import SessionInfo, session_in
from muliao.sessions.kinds import KindPolicy, SessionKind, policy_of
from muliao.sessions.live import RunDeltaEvent, RunEvent, RunItemEvent, SavedEvent
from muliao.sessions.pending import CallResult, call_result
from muliao.sessions.questions import QuestionAnswer
from muliao.sessions.service import default_title
from muliao.sessions.transcript import Item
from muliao.sessions.work import SessionWork

__all__ = [
    "SessionActivity",
    "WorkState",
    "KindPolicy",
    "SessionKind",
    "policy_of",
    "RunDeltaEvent",
    "RunEvent",
    "RunItemEvent",
    "SavedEvent",
    "CallResult",
    "call_result",
    "QuestionAnswer",
    "default_title",
    "Item",
    "AgentHistory",
    "Halted",
    "Inbound",
    "NewSession",
    "SessionInfo",
    "check_receivable_in",
    "SessionWork",
    "hold_registered_in",
    "receive_command",
    "session_in",
]
