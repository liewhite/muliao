"""会话领域的装配：组合根只调用这里，不自己拼会话领域的内部对象（docs/architecture.md 2.3「组合根没有构造环」）。

分两段，因为中间隔着别的领域：
1. SessionPorts.build：先于消费者构造、别的领域要用的端口——会话忙不忙（SessionActivity）、停止会话（SessionStopper，
   由正在进行的运行的登记与 SessionHalt 组成）、委派（Threads、ThreadChecks）、到点唤醒（WakeUps）、后台任务的占用
   （SessionWork）；以及委派与到点唤醒的工具组（工具箱要先于消费者组好）。
2. SessionDomain.build：有了运行时与工具箱之后，会话门面、压缩、消费者、信箱对账，与会话领域的任务处理器。
   subscribe 一处登记会话领域的全部订阅与命令处理（后台任务与委派的结局、Receive、删除 agent 的会话清理）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset

from muliao.agent_runtime.api import AgentRuntime, Toolbox
from muliao.agents.api import AgentService
from muliao.files.api import FileStore
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.platform.tooling import RunScope
from muliao.platform.worker import RunNow, TaskHandler
from muliao.sessions.activity import SessionActivity
from muliao.sessions.compaction import CompactionHandler, Compactor
from muliao.sessions.consumer import Consumer, RunHandler
from muliao.sessions.content import ContentBuilder
from muliao.sessions.delegation import ThreadChecks
from muliao.sessions.delegation_tools import delegation_toolset
from muliao.sessions.deletion import AgentSessionsCleanup
from muliao.sessions.finish import RunFinisher
from muliao.sessions.halt import SessionHalt, SessionStopper
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK
from muliao.sessions.live import LiveSegments
from muliao.sessions.origins import OriginNotes
from muliao.sessions.reconcile import MailReconciler
from muliao.sessions.service import SessionService
from muliao.sessions.steps import Steps
from muliao.sessions.stopping import StoppableRuns
from muliao.sessions.threads import Threads
from muliao.sessions.wakeup_tools import wakeup_toolset
from muliao.sessions.wakeups import WakeUps
from muliao.sessions.work import SessionWork, handle_work_commands


@dataclass(frozen=True)
class SessionPorts:
    activity: SessionActivity
    # 正在进行的运行的登记：停止会话经它停下运行，消费者用同一份登记运行
    runs: StoppableRuns[Commit]
    halt: SessionHalt
    stopper: SessionStopper
    threads: Threads
    thread_checks: ThreadChecks
    wakeups: WakeUps
    work: SessionWork
    _db: Database
    _tz: ZoneInfo

    @classmethod
    def build(cls, db: Database, queue: TaskQueue, messaging: Messaging, tz: ZoneInfo) -> SessionPorts:
        runs: StoppableRuns[Commit] = StoppableRuns(db.exclusive)
        halt = SessionHalt(db, queue, messaging)
        stopper = SessionStopper(runs, halt)
        return cls(
            activity=SessionActivity(queue),
            runs=runs,
            halt=halt,
            stopper=stopper,
            threads=Threads(queue),
            thread_checks=ThreadChecks(db, queue),
            wakeups=WakeUps(db, queue, tz),
            work=SessionWork(db),
            _db=db,
            _tz=tz,
        )

    def delegation_toolset(self, agents: AgentService) -> FunctionToolset[RunScope]:
        return delegation_toolset(self._db, agents, self.threads, self.stopper)

    def wakeup_toolset(self) -> FunctionToolset[RunScope]:
        return wakeup_toolset(self._db, self._tz)


@dataclass(frozen=True)
class SessionDomain:
    sessions: SessionService
    compactor: Compactor
    consumer: Consumer
    mail: MailReconciler
    _ports: SessionPorts
    _db: Database
    _queue: TaskQueue

    @classmethod
    def build(
        cls,
        ports: SessionPorts,
        *,
        db: Database,
        queue: TaskQueue,
        live: LiveSegments,
        messaging: Messaging,
        run_now: RunNow,
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
            Steps(db, live, content, compactor, renders),
            RunFinisher(queue, messaging),
            ports.runs,
            ports.halt,
            compactor,
            notes,
        )
        return cls(
            sessions=SessionService(db, queue, live, messaging, ports.stopper, compactor, run_now, renders),
            compactor=compactor,
            consumer=consumer,
            mail=MailReconciler(db, queue),
            _ports=ports,
            _db=db,
            _queue=queue,
        )

    def subscribe(self, messaging: Messaging) -> None:
        """会话领域的全部订阅与命令处理：后台任务与委派的结局、运行中检查（WorkEnded / WorkCheckIn）、来信（Receive）、
        停止 thread、删除 agent 时清理它的会话。"""
        handle_work_commands(messaging, self._queue)
        self.sessions.subscribe(messaging)
        AgentSessionsCleanup(self._db, self._queue, messaging, self._ports.stopper).subscribe(messaging)

    def handlers(self, messaging: Messaging) -> Sequence[tuple[str, TaskHandler]]:
        """会话 lane 上的任务种类与处理器（组合根交给 worker 登记）。"""
        return (
            (RUN_TASK, RunHandler(self.consumer, self._queue, messaging)),
            (COMPACTION_TASK, CompactionHandler(self._db, self.compactor)),
        )
