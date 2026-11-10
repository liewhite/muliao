"""会话领域的公开接口：别的领域只经这里（与 sessions.events）使用会话领域（docs/architecture.md 第 7 节、2.3）。

- 来信：界面的 UserMessage 经 SessionService.receive；别的领域的 SenderMail 由发送方在自己的事务里 receive_command_in（校验来信、得到定向命令 Receive，在同一事务 send_in，sessions.events）；目标会话可以按发送方
  定位（SessionKey），keyed_session_in 查一个、keyed_sessions_among 查一批这样的会话；
- 后台任务：SessionWork.hold（在会话领域自己的事务里登记占用），hold_registered_in；运行中检查的间隔规则 check_interval / CheckBeats；结局与运行中检查经定向命令
  WorkEnded / WorkCheckIn（sessions.events）；
- HTTP 层用的会话门面（SessionService：会话的读写、停止、手动压缩）；别的领域停止会话经端口 StopSession（结果 Halted），返回时
  停止已生效；
- 查询：session_in（SessionInfo）、SessionActivity（会话忙不忙）、SessionRecall（热历史：一段时间的会话活动与用户说的话，
  history 领域的 activity_overview 用；会话的时间线由本领域的 session_read 工具读）；
- 会话在等的东西（Wait：后台任务、委派、到点唤醒、提问，随会话详情给出）；
- 会话来源（Origin：发送方新建会话时给出）、会话种类与 HTTP 层用的读模型、展示事件；
- 会话 lane 上的任务种类（RUN_TASK、COMPACTION_TASK：HTTP 层把各领域的任务种类组成接口里的 TaskKind）。
"""

from muliao.config import MIN_COMPACTION_THRESHOLD_TOKENS
from muliao.sessions.activity import SessionActivity, WorkState
from muliao.sessions.approvals import Choice as ApprovalChoice
from muliao.sessions.assembly import SessionDomain, SessionPorts
from muliao.sessions.halt import Halted, StopSession
from muliao.sessions.holds import registered_in as hold_registered_in
from muliao.sessions.inbound import (
    NewChat,
    NewConversation,
    SenderMail,
    SessionKey,
    UserMessage,
    receive_command_in,
)
from muliao.sessions.info import SessionInfo, keyed_session_in, keyed_sessions_among, session_in
from muliao.sessions.kinds import SessionKind
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK
from muliao.sessions.live import LiveSegment, RunDeltaEvent, RunEvent, RunItemEvent, SavedEvent
from muliao.sessions.origins import OWN_NOTES, Origin, OriginNotes
from muliao.sessions.pending import CallResult, call_result
from muliao.sessions.questions import QuestionAnswer
from muliao.sessions.recall import Activity, DaySession, RunFailure, SessionRecall, UserWords
from muliao.sessions.recall_tools import SESSION_READ, indented, place
from muliao.sessions.service import SessionService
from muliao.sessions.sources import EventSource, job_holder, job_id_of_holder, parse_source
from muliao.sessions.transcript import Item
from muliao.sessions.waits import ApprovalWait, AskWait, DelegationWait, JobWait, Wait, WakeWait
from muliao.sessions.work import CheckBeats, SessionWork, check_interval

__all__ = [
    "COMPACTION_TASK",
    "MIN_COMPACTION_THRESHOLD_TOKENS",
    "RUN_TASK",
    "OWN_NOTES",
    "Origin",
    "OriginNotes",
    "SessionActivity",
    "Halted",
    "StopSession",
    "SessionDomain",
    "SessionPorts",
    "WorkState",
    "SessionKind",
    "LiveSegment",
    "RunDeltaEvent",
    "RunEvent",
    "RunItemEvent",
    "SavedEvent",
    "CallResult",
    "call_result",
    "QuestionAnswer",
    "SessionRecall",
    "Activity",
    "DaySession",
    "RunFailure",
    "UserWords",
    "SESSION_READ",
    "indented",
    "place",
    "SessionService",
    "Item",
    "Wait",
    "JobWait",
    "DelegationWait",
    "WakeWait",
    "ApprovalChoice",
    "ApprovalWait",
    "AskWait",
    "NewChat",
    "NewConversation",
    "SenderMail",
    "UserMessage",
    "SessionInfo",
    "SessionKey",
    "keyed_session_in",
    "keyed_sessions_among",
    "receive_command_in",
    "SessionWork",
    "check_interval",
    "CheckBeats",
    "hold_registered_in",
    "session_in",
    "EventSource",
    "job_id_of_holder",
    "job_holder",
    "parse_source",
]
