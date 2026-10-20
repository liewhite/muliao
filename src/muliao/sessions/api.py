"""会话领域的公开接口：别的领域只经这里（与 sessions.events）使用会话领域（docs/domain-design.md 4.1、第 10 节）。

- 来信：receive（Inbound / NewSession），thread：Threads、validate_thread；
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
    receive,
    session_in,
    validate_thread,
)
from muliao.sessions.halt import Halted
from muliao.sessions.holds import hold_in
from muliao.sessions.holds import registered_in as hold_registered_in
from muliao.sessions.mailbox import latest_run, post_check_in
from muliao.sessions.pending import AgentAnswer, pending_in
from muliao.sessions.settlement import deliver_in
from muliao.sessions.work import AgentWork

__all__ = [
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
    "deliver_in",
    "hold_in",
    "hold_registered_in",
    "latest_run",
    "pending_in",
    "post_check_in",
    "receive",
    "session_in",
    "validate_thread",
]
