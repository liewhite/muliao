"""会话领域的公开接口：别的领域只经这里（与 sessions.events）使用会话领域（docs/architecture.md 第 7 节、2.3）。

- 来信：发送方在自己的事务里 check_receivable_in，再发出 receive_command 构造的定向命令 Receive（sessions.events）；
- 后台任务：SessionWork（登记占用、运行中检查，各在会话领域自己的事务里），hold_registered_in；结局经定向命令
  PostEvent 交回；
- 查询：session_in（SessionInfo）、session_has_pending_work_in、agent_deletion_blocked_in、AgentWork（反思用的步数与记录）；
- 会话种类与 HTTP 层用的读模型、展示事件；停止的结果：Halted。
"""

from muliao.sessions.deletion import agent_deletion_blocked_in, session_has_pending_work_in
from muliao.sessions.delivery import SessionWork
from muliao.sessions.halt import Halted
from muliao.sessions.holds import registered_in as hold_registered_in
from muliao.sessions.inbound import Inbound, NewSession, check_receivable_in, receive_command
from muliao.sessions.info import SessionInfo, session_in
from muliao.sessions.kinds import KindPolicy, SessionKind, policy_of
from muliao.sessions.live import RunDeltaEvent, RunEvent, RunItemEvent, SavedEvent
from muliao.sessions.pending import CallResult, call_result
from muliao.sessions.questions import QuestionAnswer
from muliao.sessions.service import default_title
from muliao.sessions.threads import Threads
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
    "AgentWork",
    "Halted",
    "Inbound",
    "NewSession",
    "SessionInfo",
    "Threads",
    "agent_deletion_blocked_in",
    "check_receivable_in",
    "SessionWork",
    "hold_registered_in",
    "receive_command",
    "session_has_pending_work_in",
    "session_in",
]
