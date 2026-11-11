"""会话领域的装配：组合根只调用这里，不自己拼会话领域的内部对象（docs/architecture.md 2.3「组合根没有构造环」）。

分两段，因为中间隔着别的领域；两段各自声明交给组合根的部件（platform.wiring.Parts）：
1. SessionPorts.build：先于消费者构造、别的领域要用的端口——会话忙不忙（SessionActivity）、停止会话（SessionStopper，
   由正在进行的运行的登记与 SessionHalt 组成）、委派（Threads、Delegations、ThreadChecks）、到点唤醒（WakeUps）、后台任务的占用
   （SessionWork）、热历史（SessionRecall）。会话领域的工具集（提问、委派、到点唤醒、读会话）都由这一段交出：消费者要用
   组好的工具箱，第 2 段交不了。委派与到点唤醒的运行中检查、删除 agent 时会话忙不忙的检查也在这里。
2. SessionDomain.build：有了运行时与工具箱之后，会话门面、压缩、消费者、信箱对账，与会话领域的任务处理器。
   subscribe 一处登记会话领域的全部订阅与命令处理（后台任务与委派的结局、Receive、删除 agent 的会话清理）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset

from muliao.agent_runtime.api import AgentRuntime, Toolbox
from muliao.agents.api import AgentService, DeletionBlocker
from muliao.files.api import FileStore
from muliao.platform.checks import PeriodicCheck
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.platform.tooling import RunScope
from muliao.platform.wiring import Loop, Parts
from muliao.platform.worker import TaskHandler
from muliao.sessions.activity import SessionActivity
from muliao.sessions.ask_user import ask_user_toolset
from muliao.sessions.compaction import CompactionHandler, Compactor
from muliao.sessions.consumer import Consumer, RunHandler
from muliao.sessions.content import ContentBuilder
from muliao.sessions.delegation import Delegations, ThreadChecks
from muliao.sessions.delegation_tools import delegation_toolset
from muliao.sessions.deletion import AgentSessionsCleanup
from muliao.sessions.finish import RunFinisher
from muliao.sessions.halt import SessionHalt, SessionStopper
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK
from muliao.sessions.live import LiveSegments
from muliao.sessions.origins import OriginNotes
from muliao.sessions.recall import SessionRecall
from muliao.sessions.recall_tools import recall_toolset
from muliao.sessions.reconcile import MailReconciler
from muliao.sessions.service import SessionService
from muliao.sessions.steps import Steps
from muliao.sessions.stopping import StoppableRuns
from muliao.sessions.threads import Threads
from muliao.sessions.wakeup_tools import wakeup_toolset
from muliao.sessions.wakeups import WakeUps
from muliao.sessions.work import SessionWork, handle_work_commands


@dataclass(frozen=True)
class SessionPorts(Parts):
    activity: SessionActivity
    # 正在进行的运行的登记：停止会话经它停下运行，消费者用同一份登记运行
    runs: StoppableRuns[Commit]
    halt: SessionHalt
    stopper: SessionStopper
    threads: Threads
    delegations: Delegations
    thread_checks: ThreadChecks
    wakeups: WakeUps
    work: SessionWork
    # 热历史：一段时间里的会话活动与会话的时间线（history 领域的 activity_overview 也用）
    recall: SessionRecall
    _tools: tuple[FunctionToolset[RunScope], ...]

    @classmethod
    def build(
        cls,
        db: Database,
        queue: TaskQueue,
        messaging: Messaging,
        tz: ZoneInfo,
        agents: AgentService,
        tool_output_max_chars: int,
    ) -> SessionPorts:
        runs: StoppableRuns[Commit] = StoppableRuns(db.exclusive)
        halt = SessionHalt(db, queue, messaging)
        stopper = SessionStopper(runs, halt)
        threads = Threads(queue)
        delegations = Delegations(queue, threads)
        recall = SessionRecall(db, queue)
        return cls(
            activity=SessionActivity(queue),
            runs=runs,
            halt=halt,
            stopper=stopper,
            threads=threads,
            delegations=delegations,
            thread_checks=ThreadChecks(db, queue),
            wakeups=WakeUps(db, queue, tz),
            work=SessionWork(db),
            recall=recall,
            _tools=(
                ask_user_toolset(),
                delegation_toolset(db, agents, threads, delegations, stopper),
                wakeup_toolset(db, tz),
                recall_toolset(recall, agents, tz, tool_output_max_chars),
            ),
        )

    @property
    def deletion_blockers(self) -> Mapping[str, DeletionBlocker]:
        """删除（或归档）agent 时会话领域的检查：它的会话有忙的就拦下（组合根交给 agents.api.AgentLifecycle）。"""
        return {"sessions": self.activity.blocks_agent_deletion_in}

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        return self._tools

    def checks(self) -> Sequence[PeriodicCheck]:
        return (self.thread_checks, self.wakeups)


@dataclass(frozen=True)
class SessionDomain(Parts):
    """工具集不在这一段：见 SessionPorts。"""

    sessions: SessionService
    compactor: Compactor
    consumer: Consumer
    mail: MailReconciler
    _ports: SessionPorts
    _db: Database
    _queue: TaskQueue
    _messaging: Messaging
    _runtime: AgentRuntime

    @classmethod
    def build(
        cls,
        ports: SessionPorts,
        *,
        db: Database,
        queue: TaskQueue,
        live: LiveSegments,
        messaging: Messaging,
        runtime: AgentRuntime,
        toolbox: Toolbox,
        files: FileStore,
        agents: AgentService,
        notes: OriginNotes,
        compaction_threshold_tokens: int,
        compaction_keep_steps: int,
    ) -> SessionDomain:
        compactor = Compactor(
            db, runtime, queue, threshold_tokens=compaction_threshold_tokens, keep_steps=compaction_keep_steps
        )
        content = ContentBuilder(files, agents.workspace)
        # 工具的展示规则：历史（SessionService.detail）与运行中的条目（Steps）用同一份
        renders = toolbox.renders()
        consumer = Consumer(
            db,
            queue,
            runtime,
            toolbox,
            live,
            content,
            Steps(db, live, content, compactor, renders, toolbox.redaction()),
            RunFinisher(queue, messaging),
            ports.runs,
            ports.halt,
            notes,
        )
        return cls(
            sessions=SessionService(db, queue, live, messaging, ports.stopper, compactor, renders),
            compactor=compactor,
            consumer=consumer,
            mail=MailReconciler(db, queue),
            _ports=ports,
            _db=db,
            _queue=queue,
            _messaging=messaging,
            _runtime=runtime,
        )

    def subscribe(self, messaging: Messaging) -> None:
        """会话领域的全部订阅与命令处理：后台任务与委派的结局、运行中检查（WorkEnded / WorkCheckIn）、来信（Receive）、
        停止 thread、删除 agent 时清理它的会话。"""
        handle_work_commands(messaging, self._queue)
        self.sessions.subscribe(messaging)
        AgentSessionsCleanup(self._db, self._queue, messaging, self._ports.stopper).subscribe(messaging)

    def handlers(self) -> Sequence[tuple[str, TaskHandler]]:
        """会话 lane 上的任务种类与处理器。"""
        return (
            (RUN_TASK, RunHandler(self.consumer, self._queue, self._messaging)),
            (COMPACTION_TASK, CompactionHandler(self._db, self.compactor, self._runtime)),
        )

    def loops(self) -> Sequence[tuple[str, Loop]]:
        """信箱对账：收敛投递链上漏掉的信（sessions.reconcile）。"""
        return (("mail", self.mail.run_forever),)
