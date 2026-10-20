"""会话领域的公开接口：别的领域只经这里（与 sessions.events）使用会话领域（docs/domain-design.md 4.1、第 10 节）。

- 来信：发送方在自己的事务里 check_receivable_in，再发出 receive_command 构造的定向命令 Receive（sessions.events）；
  thread：Threads、validate_thread；
- 占用与结局：hold_in、hold_registered_in、deliver_in、pending_in、AgentAnswer；运行中检查：post_check_in；
- 查询：session_in（SessionInfo）、latest_run、agent_busy_in、AgentWork（反思用的步数与记录）；
- 停止的结果：Halted。
"""

from muliao.sessions.conversation import (
    Inbound,
    NewSession,
    Received,
    SessionInfo,
    Threads,
    ThreadView,
    agent_busy_in,
    check_receivable_in,
    receive_command,
    session_busy_in,
    session_in,
    validate_thread,
)
from muliao.sessions.halt import Halted
from muliao.sessions.holds import hold_in
from muliao.sessions.holds import registered_in as hold_registered_in
from muliao.sessions.kinds import KindPolicy, SessionKind, policy_of
from muliao.sessions.live import RunDeltaEvent, RunEvent, RunItemEvent, SavedEvent
from muliao.sessions.mailbox import latest_run, post_check_in
from muliao.sessions.pending import AgentAnswer, CallResult, call_result, pending_in
from muliao.sessions.questions import QuestionAnswer
from muliao.sessions.service import default_title
from muliao.sessions.settlement import deliver_in
from muliao.sessions.transcript import Item
from muliao.sessions.work import AgentWork

__all__ = [
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
    "AgentAnswer",
    "AgentWork",
    "Halted",
    "Inbound",
    "NewSession",
    "Received",
    "SessionInfo",
    "ThreadView",
    "Threads",
    "agent_busy_in",
    "check_receivable_in",
    "deliver_in",
    "hold_in",
    "hold_registered_in",
    "latest_run",
    "pending_in",
    "post_check_in",
    "receive_command",
    "session_busy_in",
    "session_in",
    "validate_thread",
]
