"""后台任务：agent 在后台跑的 shell 命令，会话拥有的异步工作的一种（platform.checks）。做法同 Claude Code 的后台 bash：
不占住运行、不设时限，结束时往发起它的会话的信箱投事件信件（sessions.mailbox），运行期间每隔一段时间发一个检查事件，
由 agent 决定继续等还是结束它。

- 输出（stdout 与 stderr 合并）逐块脱敏后写进 agent 工作目录的 jobs/<id>.log，agent 用 read_file 读完整输出；
  事件里只带最后几行。
- 进程自成进程组（shell.spawn_bash），job_kill 与关停杀掉整棵进程树。
- 结束与投递完成事件在同一个事务里：任务记为结束，事件就一定在信箱里（会话被停止时除外：结局照记，不投信）。
- 以进程退出为准（不是输出管道关闭）：退出后在有限时间内读完剩余输出，后台孙进程一直占着管道也不会让任务结束不了。
- 不再有人监督的任务一律记为被中断并告诉 agent，同一条路径（_interrupt_in）：正常关停时杀掉进程后；服务意外退出
  （被强杀、崩溃）后启动时（recover）——进程自成进程组、可能还活着，如实告知，不盲杀（pid 可能已被复用）；以及运行中
  监督意外结束（如记下结局时数据库失败），由每次检查前的对账发现。
- 任务对 agent 可见（工作目录本来就是 agent 级的，job_status / job_kill 按 agent 检查归属），事件归发起它的会话。
  日志大小不设上限（按「不限制」的决定）。
- 反思与临时 agent 没有会话、没有信箱接收事件，不能开后台任务。
"""

from __future__ import annotations

import asyncio
import codecs
import logging
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import TaskGroup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import Conflict, NotFound
from muliao.jobs.tables import Job, JobStatus
from muliao.platform.checks import post_check_in
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.shell import DRAIN_SECONDS, kill_group, redact, spawn_bash
from muliao.platform.tasks import Clock, Commit, TaskQueue, no_commit
from muliao.sessions.events import PostEvent, SessionStopped
from muliao.sessions.holds import get_in as get_hold_in
from muliao.sessions.holds import hold_in
from muliao.sessions.tables import Session
from muliao.vault.service import Vault

log = logging.getLogger(__name__)

JOBS_DIR = "jobs"
_CHUNK = 65536
_TAIL_LINES = 30
_TAIL_BYTES = 16384
_EXIT_POLL_SECONDS = 0.2


def job_source(job_id: int) -> str:
    """任务发出的事件的来源（也用来判断它是否已有事件在队列里等着），也是它在会话上的占用者（sessions.holds）。"""
    return f"job:{job_id}"


def _job_id(holder: str) -> int | None:
    """占用者是后台任务时的任务 id。"""
    prefix, _, rest = holder.partition(":")
    return int(rest) if prefix == "job" and rest.isdigit() else None


def _start_failure(error: BaseException) -> str:
    return f"it could not be started ({type(error).__name__}: {error})"


def _duration(start: datetime, end: datetime) -> str:
    seconds = int((end - start).total_seconds())
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s" if hours else f"{minutes}m{seconds:02d}s"


def tail(path: Path) -> str:
    """日志的最后几行（读文件末尾一段，不整个读进来）。"""
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - _TAIL_BYTES))
        data = f.read()
    lines = data.decode(errors="replace").splitlines()
    if size > _TAIL_BYTES:
        # 第一行可能被截在中间
        lines = lines[1:]
    return "\n".join(lines[-_TAIL_LINES:])


@dataclass(frozen=True)
class JobInfo:
    id: int
    command: str
    status: JobStatus
    exit_code: int | None
    log_path: str
    running_for: str
    tail: str


@dataclass
class _Running:
    proc: asyncio.subprocess.Process
    # job_kill 设置：进程结束后记为 KILLED 而不是 EXITED
    killed: bool = False
    # 监督者自己的取消作用域：只取消这一个监督者（启动时记录写不进去）；关停取消的是整个任务组
    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    # 监督者开始执行 / 结束（结局写完、失败或被取消）
    started: bool = False
    finished: anyio.Event = field(default_factory=anyio.Event)


# 被取消后的清理（结束监督者、删掉没起来的任务行）最多等这么久
_CLEANUP_SECONDS = 2 * DRAIN_SECONDS


class JobRunner:
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        vault: Vault,
        workspace_of: Callable[[str], Path],
        check_seconds: float,
        messaging: Messaging,
        clock: Clock = utcnow,
    ):
        self._db = db
        self._queue = queue
        self._messaging = messaging
        self._vault = vault
        self._workspace_of = workspace_of
        self.check_seconds = check_seconds
        self._clock = clock
        self._running: dict[int, _Running] = {}
        # 正在启动（行已写入、监督者还没接手）的任务
        self._starting: set[int] = set()
        # 监督者所在的任务组：服务运行期间（running）存在
        self._group: TaskGroup | None = None

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """后台任务的监督者都在这个任务组里。退出时先按关停结束所有进程（stop），再取消剩下的监督者。"""
        async with anyio.create_task_group() as group:
            self._group = group
            try:
                yield
            finally:
                with anyio.CancelScope(shield=True):
                    await self.stop()
                self._group = None
                group.cancel_scope.cancel()

    async def start(
        self,
        *,
        session_id: str,
        agent_id: str,
        command: str,
        secrets: list[str],
        awaited: bool,
    ) -> Job:
        """awaited：发起它的那次运行里用户是否在等回答（完成通知随它推送）。只在有会话的运行里能开
        （tools.workspace.session_workspace_toolset）。"""
        group = self._group
        if group is None:
            raise RuntimeError("background jobs can only start while the job runner is running")
        # vault 自己开事务，不能放进下面的事务
        values = await self._vault.resolve_many(agent_id, secrets)
        job: Job | None = None
        try:
            async with self._db.transaction() as s:
                session = await s.get(Session, session_id)
                if session is None:
                    raise NotFound(f"session {session_id!r} not found")
                job = Job(
                    session_id=session_id,
                    agent_id=agent_id,
                    command=command,
                    secrets=list(secrets),
                    awaited=awaited,
                    log_path="",
                    started_at=self._clock(),
                )
                s.add(job)
                await s.flush()
                job.log_path = f"{JOBS_DIR}/{job.id}.log"
                # 正在启动：对账不把它当成没人监督的（无论启动成败，结束时撤销）。在插入的事务里登记，行一出现就有标记
                self._starting.add(job.id)
        except BaseException:
            # 插入没有提交：撤销标记
            if job is not None:
                self._starting.discard(job.id)
            raise
        try:
            await self._launch(job, values, group)
        finally:
            self._starting.discard(job.id)
        log.info("background job started", extra={"job_id": job.id, "session_id": session_id, "pid": job.pid})
        return job

    async def _launch(self, job: Job, values: Mapping[str, str], group: TaskGroup) -> None:
        """起进程与监督者并记下 pid。任何一步失败或被取消（这次运行被停止）都不能留下没人能结束的进程：清理在
        屏蔽作用域里做完；清理超时则记 error，这一行留在 running，下一次检查的对账把它记为被中断并告诉 agent。"""
        workspace = self._workspace_of(job.agent_id)
        log_file = workspace / job.log_path
        try:
            # 先在会话上登记占用（会话据此知道工作还没做完），再起进程
            async with self._db.transaction() as s:
                await hold_in(s, job.session_id, job_source(job.id))
            await asyncio.to_thread(log_file.parent.mkdir, parents=True, exist_ok=True)
            proc = await spawn_bash(job.command, cwd=workspace, secrets=values, merge_stderr=True)
        except BaseException as e:
            await self._cleanup(job.id, "spawn", self._close_unstarted(job.id, _start_failure(e)))
            raise
        handle = _Running(proc)
        try:
            # 进程一起来就有人管：先登记、起监督者，再记 pid
            self._running[job.id] = handle
            group.start_soon(self._supervise, job.id, handle, log_file, values, name=f"job-{job.id}")
            await self._record_pid(job, proc.pid)
        except BaseException:
            # 取消监督者会杀掉进程组；监督者没起来（任务组已关闭）时在这里杀
            kill_group(proc.pid)
            self._running.pop(job.id, None)
            await self._cleanup(job.id, "record pid", self._stop_and_close(job.id, handle))
            raise

    async def _record_pid(self, job: Job, pid: int) -> None:
        async with self._db.transaction() as s:
            row = await s.get(Job, job.id)
            if row is None:
                raise RuntimeError(f"job {job.id} vanished right after it was created")
            row.pid = job.pid = pid

    async def _stop_and_close(self, job_id: int, handle: _Running) -> None:
        handle.scope.cancel()
        if handle.started:
            await handle.finished.wait()
        await self._close_unstarted(job_id, "its process id could not be recorded (see server log)")

    async def _close_unstarted(self, job_id: int, reason: str) -> None:
        async with self._db.transaction() as s:
            job = await s.get(Job, job_id)
            if job is not None and job.status == JobStatus.RUNNING:
                await self._close_unsupervised_in(s, job, reason, may_be_alive=False)

    async def _close_unsupervised_in(
        self, s: AsyncSession, job: Job, reason: str, *, may_be_alive: bool
    ) -> None:
        """没有监督者的任务行（启动失败、监督意外结束）的唯一收敛规则：会话上还没登记占用，说明任务没开始过，删掉
        这一行；登记过了，就记为被中断，交回会话并释放占用。"""
        if await get_hold_in(s, job.session_id, job_source(job.id)) is None:
            await s.delete(job)
            log.info("background job that never started discarded", extra={"job_id": job.id})
            return
        await self._interrupt_in(s, job, reason, may_be_alive=may_be_alive)

    async def _cleanup(self, job_id: int, stage: str, cleanup: Coroutine[Any, Any, None]) -> None:
        """启动失败后的清理：屏蔽取消、设时限；做不完就记下来，交给对账。"""
        with anyio.CancelScope(shield=True), anyio.move_on_after(_CLEANUP_SECONDS) as limited:
            await cleanup
        if limited.cancelled_caught:
            log.error(
                "cleanup after a failed background job start timed out; the check will reconcile it",
                extra={"job_id": job_id, "stage": stage},
            )

    async def _supervise(
        self, job_id: int, handle: _Running, log_file: Path, secrets: Mapping[str, str]
    ) -> None:
        """看着进程直到结局写进库。「有人监督」（_running）一直保持到结局写完（成功、失败或被取消），
        对账才不会把正在记结局的任务当成没人管的。这是每个监督者的异常边界：意外异常只记日志，这一行停在
        running，由 check 的对账记为被中断并告诉 agent，不影响别的监督者与运行。"""
        handle.started = True
        try:
            with handle.scope:
                status, exit_code, note = await self._watch(job_id, handle, log_file, secrets)
                # 写结局失败（如数据库暂时不可用）时这一行停在 running；撤销登记后由 check 的对账收敛
                await self._finish(job_id, handle, status, exit_code, note)
        except Exception:
            log.exception("background job supervisor crashed", extra={"job_id": job_id})
        finally:
            self._running.pop(job_id, None)
            handle.finished.set()

    async def _watch(
        self, job_id: int, handle: _Running, log_file: Path, secrets: Mapping[str, str]
    ) -> tuple[JobStatus, int | None, str | None]:
        """以进程退出为准：退出后在有限时间内把管道里剩下的输出读完（脱离进程组的后代可能一直持有管道）。
        返回结局（状态、退出码、补充说明或被中断的原因）。"""
        proc = handle.proc
        recorder = _Recorder(proc, log_file, secrets)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(recorder.run)
                exit_code = await _exit_code(proc)
                with anyio.move_on_after(DRAIN_SECONDS):
                    await recorder.done.wait()
                drained = recorder.done.is_set()
                # 后代还占着管道时不再等它的输出
                group.cancel_scope.cancel()
        except BaseException:
            # 关停（stop 随后记为被中断）、启动时记录写不进去，或意外失败：进程不随服务退出，在这里杀掉
            kill_group(proc.pid)
            raise
        if recorder.error is not None:
            # 读输出、写日志意外失败（记录者已杀掉进程）：记为被中断，失败原因进日志，agent 收到事件
            log.error(
                "background job output could not be recorded",
                exc_info=recorder.error,
                extra={"job_id": job_id},
            )
            return JobStatus.INTERRUPTED, None, "its output could not be recorded (see server log)"
        ended = JobStatus.KILLED if handle.killed else JobStatus.EXITED
        if drained:
            return ended, exit_code, None
        return (
            ended,
            exit_code,
            "Processes it left running in the background still hold its output; their later output is not recorded.",
        )

    async def _finish(
        self, job_id: int, handle: _Running, status: JobStatus, exit_code: int | None, note: str | None
    ) -> None:
        async with self._db.transaction() as s:
            job = await s.get(Job, job_id)
            if job is None:
                # 任务行只进入终态、不删除（没有跨领域外键，会话删除时也留着）：不见了是 bug
                raise RuntimeError(f"job {job_id} vanished while it was running")
            if job.status != JobStatus.RUNNING:
                # 结局只记一次：监督者在写完之前一直登记着，对账与关停不会抢先记
                raise RuntimeError(f"job {job_id} already ended as {job.status}, cannot record {status}")
            now = self._clock()
            job.status, job.exit_code, job.finished_at = status, exit_code, now
            text = _final_text(job, now, tail(self._workspace_of(job.agent_id) / job.log_path), note)
            self._report_in(s, job, text)
        log.info(
            "background job finished", extra={"job_id": job_id, "status": status, "exit_code": exit_code}
        )

    async def _interrupt_in(self, s: AsyncSession, job: Job, reason: str, *, may_be_alive: bool) -> None:
        """把一个不再有人监督的任务记为被中断并告诉 agent。recover（崩溃后启动）、对账（监督意外结束）、关停共用。"""
        now = self._clock()
        job.status, job.finished_at = JobStatus.INTERRUPTED, now
        note = reason
        if may_be_alive and job.pid is not None:
            # 不去杀它：pid 可能已被系统复用给别的进程。交给 agent 用命令核实
            note += (
                f". Its process group {job.pid} may still be running: check with `pgrep -g {job.pid}` and stop it "
                f"with `kill -- -{job.pid}` if it should not keep running"
            )
        text = _final_text(job, now, tail(self._workspace_of(job.agent_id) / job.log_path), note)
        self._report_in(s, job, text)

    def _report_in(self, s: AsyncSession, job: Job, text: str) -> None:
        """结局与「一定交回会话」同一个事务（定向命令）：会话投一封事件信件并释放占用；会话已被停止（占用已清掉）
        或已删除时不投。"""
        self._messaging.send_in(
            s,
            PostEvent(
                session_id=job.session_id,
                source=job_source(job.id),
                content=text,
                awaited=job.awaited,
                key=f"{job_source(job.id)}:end",
                release=job_source(job.id),
            ),
        )

    async def kill(self, job_id: int, agent_id: str) -> None:
        """结束这个 agent 的一个运行中的任务；结果（KILLED）随后作为事件到达。"""
        async with self._db.transaction() as s:
            job = await _own(s, job_id, agent_id)
            if job.status != JobStatus.RUNNING:
                raise Conflict(f"job {job_id} is not running (it {job.status})")
        handle = self._running.get(job_id)
        if handle is None or handle.proc.returncode is not None:
            # 进程刚好自己退出了，结局正在记下
            raise Conflict(f"job {job_id} has just ended; its final status will arrive as an event")
        handle.killed = True
        kill_group(handle.proc.pid)
        log.info("background job kill requested", extra={"job_id": job_id})

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(SessionStopped, "jobs.stopped", self.on_stopped)

    async def on_stopped(self, event: SessionStopped, _delivery: Delivery) -> Commit:
        """会话被用户停止：结束被清掉的占用对应的后台任务（同 job_kill），结局照常由监督者记下（KILLED）；会话的
        占用已经清掉，结局不再投信。没有监督者的（监督意外结束）由 check 的对账收敛。"""
        stopped = []
        for holder in event.cleared:
            job_id = _job_id(holder)
            if job_id is None:
                continue
            handle = self._running.get(job_id)
            if handle is not None and handle.proc.returncode is None:
                handle.killed = True
                kill_group(handle.proc.pid)
                stopped.append(job_id)
        if stopped:
            log.info(
                "background jobs stopped with their session",
                extra={"session_id": event.session_id, "job_ids": stopped},
            )
        return no_commit

    async def info(self, job_id: int, agent_id: str) -> JobInfo:
        async with self._db.transaction() as s:
            job = await _own(s, job_id, agent_id)
        end = job.finished_at or self._clock()
        return JobInfo(
            id=job.id,
            command=job.command,
            status=job.status,
            exit_code=job.exit_code,
            log_path=job.log_path,
            running_for=_duration(job.started_at, end),
            tail=await asyncio.to_thread(tail, self._workspace_of(job.agent_id) / job.log_path),
        )

    async def check(self) -> int:
        """给每个运行中的任务发一个检查事件；它上一条事件还在队列里等着时不再发（不堆积）。返回发出的个数。

        先对账：记为运行中、却不在本进程监督之下、也不在启动中的任务（记下结局时失败、监督意外结束、启动失败后的
        清理没做完），按 recover 的同一套逻辑记为被中断并告诉 agent。"""
        async with self._db.transaction() as s:
            orphans = (
                await s.scalars(
                    select(Job).where(
                        Job.status == JobStatus.RUNNING,
                        Job.id.not_in([*self._running, *self._starting]),
                    )
                )
            ).all()
            for job in orphans:
                await self._close_unsupervised_in(
                    s, job, "its supervision ended unexpectedly (see server log)", may_be_alive=True
                )
        if orphans:
            log.warning(
                "unsupervised background jobs marked interrupted", extra={"job_ids": [j.id for j in orphans]}
            )
        async with self._db.transaction() as s:
            running = list((await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING))).all())
        tails = {
            j.id: await asyncio.to_thread(tail, self._workspace_of(j.agent_id) / j.log_path) for j in running
        }
        posted = 0
        async with self._db.transaction() as s:
            for stale in running:
                job = await s.get(Job, stale.id)
                if job is None or job.status != JobStatus.RUNNING:
                    continue
                text = _check_text(job, self._clock(), tails[job.id])
                posted += await post_check_in(
                    s, self._queue, job.session_id, job_source(job.id), job_source(job.id), text
                )
        return posted

    async def recover(self) -> None:
        """启动时（还没有任务在跑）：正常关停已把任务记为被中断，这里剩下的是服务意外退出（被强杀、崩溃）时还在跑的。
        进程自成进程组，不随服务退出，可能还活着：如实告诉 agent，不盲杀（pid 可能已被复用）。"""
        async with self._db.transaction() as s:
            stale = list((await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING))).all())
            for job in stale:
                await self._close_unsupervised_in(
                    s, job, "the service stopped unexpectedly", may_be_alive=True
                )
        if stale:
            log.warning(
                "background jobs left by an unexpected stop", extra={"job_ids": [j.id for j in stale]}
            )

    async def stop(self) -> None:
        """正常关停：杀掉所有后台任务的进程组，记为被中断并告诉 agent（事件在重启后处理）。"""
        handles = dict(self._running)
        for handle in handles.values():
            if handle.proc.returncode is not None:
                # 进程已经自己结束、正在记结局：给它时间写完，保留真实的退出码
                continue
            kill_group(handle.proc.pid)
            handle.scope.cancel()
        # 进程退出后监督者自己还要最多 DRAIN_SECONDS 读完剩余输出，再写结局；超时的取消掉
        with anyio.move_on_after(_CLEANUP_SECONDS):
            for handle in handles.values():
                await handle.finished.wait()
        if forced := [job_id for job_id, h in handles.items() if not h.finished.is_set()]:
            log.warning("background job supervisors cancelled at shutdown", extra={"job_ids": forced})
        for handle in handles.values():
            handle.scope.cancel()
        with anyio.move_on_after(_CLEANUP_SECONDS) as limited:
            for handle in handles.values():
                await handle.finished.wait()
        if limited.cancelled_caught:
            log.error(
                "background job supervisors did not end after cancellation",
                extra={"job_ids": [job_id for job_id, h in handles.items() if not h.finished.is_set()]},
            )
        if not handles:
            return
        async with self._db.transaction() as s:
            for job_id in handles:
                job = await s.get(Job, job_id)
                if job is not None and job.status == JobStatus.RUNNING:
                    await self._interrupt_in(s, job, "the service was stopped", may_be_alive=False)
        log.info("background jobs stopped with the service", extra={"job_ids": list(handles)})


async def _own(s, job_id: int, agent_id: str) -> Job:  # noqa: ANN001
    job = await s.get(Job, job_id)
    if job is None or job.agent_id != agent_id:
        raise NotFound(f"job {job_id} not found")
    return job


def _final_text(job: Job, now: datetime, last: str, note: str | None) -> str:
    """note：被中断时是原因；退出、被结束时是补充说明（如后代还占着输出），没有为 None。"""
    took = _duration(job.started_at, now)
    if job.status == JobStatus.EXITED:
        head = f"Background job {job.id} finished with exit code {job.exit_code} after {took}."
    elif job.status == JobStatus.KILLED:
        head = f"Background job {job.id} was killed (job_kill) after {took}."
    else:
        head = f"Background job {job.id} was interrupted after {took}: {note}. It may have partly run."
        note = None
    extra = f"\nNote: {note}" if note else ""
    return f"{head}{extra}\nCommand: {job.command}\n{_output(job, last)}"


async def _exit_code(proc: asyncio.subprocess.Process) -> int:
    """进程退出时的退出码。不用 Process.wait()：在进程退出前开始等的话，它要等所有管道都关闭才返回（CPython
    base_subprocess 只在管道全部断开后唤醒等待者），后代一直占着管道就一直等。returncode 在进程退出时就已设置。"""
    # 轮询：退出的通知（协议的 process_exited）只在 asyncio 内部对象上，不为它去碰私有属性
    while proc.returncode is None:  # noqa: ASYNC110
        await asyncio.sleep(_EXIT_POLL_SECONDS)
    return proc.returncode


def _safe_cut(text: str, cut: int, secrets: Mapping[str, str]) -> int:
    """把切口挪到任何 secret 出现位置之前：切口两侧分开脱敏时，跨在切口上的 secret 会漏出明文。
    挪一次可能让别的 secret（或同一个 secret 的另一次出现）跨到新切口上，所以挪到不再变化为止。"""
    while True:
        moved = cut
        for value in secrets.values():
            start = text.find(value, max(0, moved - len(value) + 1))
            while start != -1 and start < moved:
                if start + len(value) > moved:
                    moved = start
                start = text.find(value, start + 1)
        if moved == cut:
            return cut
        cut = moved


class _Recorder:
    """把输出写进日志。意外失败时杀掉进程组（进程不能没人管地留着），失败交给监督者记为被中断。"""

    def __init__(self, proc: asyncio.subprocess.Process, log_file: Path, secrets: Mapping[str, str]):
        self._proc = proc
        self._log_file = log_file
        self._secrets = secrets
        self.done = anyio.Event()
        self.error: Exception | None = None

    async def run(self) -> None:
        try:
            await _record(self._proc, self._log_file, self._secrets)
        except Exception as e:
            self.error = e
            kill_group(self._proc.pid)
        finally:
            self.done.set()


async def _record(proc: asyncio.subprocess.Process, log_file: Path, secrets: Mapping[str, str]) -> None:
    """把输出逐块脱敏后追加到日志：按行写出（secret 不会被截在两块之间）；超长的一行攒到上限也写出去，
    切口避开 secret（_safe_cut）。增量解码，多字节字符不会被块边界切坏。"""
    assert proc.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    with log_file.open("a", encoding="utf-8") as out:
        while chunk := await proc.stdout.read(_CHUNK):
            pending += decoder.decode(chunk)
            complete, sep, pending = pending.rpartition("\n")
            if sep:
                out.write(redact(complete + sep, secrets))
            if len(pending) > _CHUNK:
                cut = _safe_cut(
                    pending, len(pending) - max((len(v) for v in secrets.values()), default=0), secrets
                )
                out.write(redact(pending[:cut], secrets))
                pending = pending[cut:]
            out.flush()
        pending += decoder.decode(b"", final=True)
        if pending:
            out.write(redact(pending, secrets))


def _check_text(job: Job, now: datetime, last: str) -> str:
    return (
        f"Background job {job.id} is still running ({_duration(job.started_at, now)} so far).\n"
        f"Command: {job.command}\n{_output(job, last)}\n"
        f"If it looks stuck or is no longer needed, call job_kill({job.id}); otherwise just keep waiting, "
        "you will get an event when it finishes."
    )


def _output(job: Job, last: str) -> str:
    shown = f"Last lines of output:\n{last}" if last else "No output yet."
    return f"{shown}\nFull output: {job.log_path} (in your workspace)"
