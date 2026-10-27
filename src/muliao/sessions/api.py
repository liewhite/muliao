"""会话领域的公开接口：别的领域只经这里（与 sessions.events）使用会话领域（docs/architecture.md 第 7 节、2.3）。

- 来信：界面的 UserMessage 经 SessionService.receive；别的领域的 SenderMail 由发送方在自己的事务里 receive_command_in（校验来信、得到定向命令 Receive，在同一事务 send_in，sessions.events）；目标会话可以按发送方
  定位（SessionKey），keyed_sessions_among 查这样的会话；
- 后台任务：SessionWork.hold（在会话领域自己的事务里登记占用），hold_registered_in；运行中检查的间隔规则 DEFAULT_CHECK_SECONDS / check_due；结局与运行中检查经定向命令
  WorkEnded / WorkCheckIn（sessions.events）；
- HTTP 层用的会话门面（SessionService：会话的读写、停止、手动压缩）；
- 查询：session_in（SessionInfo）、SessionActivity（会话忙不忙）；
- 会话来源（Origin：发送方新建会话时给出）、会话种类与 HTTP 层用的读模型、展示事件；
- 会话 lane 上的任务种类（RUN_TASK、COMPACTION_TASK：HTTP 层把各领域的任务种类组成接口里的 TaskKind）。
"""

from muliao.sessions.activity import SessionActivity, WorkState
from muliao.sessions.assembly import SessionDomain, SessionPorts
from muliao.sessions.holds import registered_in as hold_registered_in
from muliao.sessions.inbound import (
    NewChat,
    NewConversation,
    SenderMail,
    SessionKey,
    UserMessage,
    receive_command_in,
)
from muliao.sessions.info import SessionInfo, keyed_sessions_among, session_in
from muliao.sessions.kinds import SessionKind, policy_of
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK
from muliao.sessions.live import LiveRun, RunDeltaEvent, RunEvent, RunItemEvent, SavedEvent
from muliao.sessions.origins import OWN_NOTES, Origin, OriginNotes
from muliao.sessions.pending import CallResult, call_result
from muliao.sessions.questions import QuestionAnswer
from muliao.sessions.service import SessionService
from muliao.sessions.sources import EventSource, job_id_of, job_source, parse_source
from muliao.sessions.transcript import Item
from muliao.sessions.work import DEFAULT_CHECK_SECONDS, SessionWork, check_due

__all__ = [
    "COMPACTION_TASK",
    "RUN_TASK",
    "OWN_NOTES",
    "Origin",
    "OriginNotes",
    "SessionActivity",
    "SessionDomain",
    "SessionPorts",
    "WorkState",
    "SessionKind",
    "policy_of",
    "LiveRun",
    "RunDeltaEvent",
    "RunEvent",
    "RunItemEvent",
    "SavedEvent",
    "CallResult",
    "call_result",
    "QuestionAnswer",
    "SessionService",
    "Item",
    "NewChat",
    "NewConversation",
    "SenderMail",
    "UserMessage",
    "SessionInfo",
    "SessionKey",
    "keyed_sessions_among",
    "receive_command_in",
    "SessionWork",
    "DEFAULT_CHECK_SECONDS",
    "check_due",
    "hold_registered_in",
    "session_in",
    "EventSource",
    "job_id_of",
    "job_source",
    "parse_source",
]
