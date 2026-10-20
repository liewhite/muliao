"""后台任务：agent 在后台跑的 shell 命令。做法同 Claude Code 的后台 bash：不占住这一轮、不设时限，
结束时给发起它的会话发事件（runtime.events），运行期间每隔一段时间发一个检查事件，由 agent 决定继续等还是结束它。

- 输出（stdout 与 stderr 合并）逐块脱敏后写进 agent 工作目录的 jobs/<id>.log，agent 用 read_file 读完整输出；
  事件里只带最后几行。
- 进程自成进程组（shell.spawn_bash），job_kill 与关停杀掉整棵进程树。
- 结束与投递完成事件在同一个事务里：任务记为结束，事件就一定在队列里。
- 服务重启时进程随服务一起没了（正常关停时这里主动杀掉）：启动时把还记为运行中的任务记为被中断，并发事件告诉
  agent（recover）。不接管旧进程。
- 私有 thread、反思与临时 agent 没有自己的队列轮次来处理事件，不能开后台任务。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.models import Job, JobStatus, ReplyTo, Session, SessionEvent
from muliao.runtime.events import post_in
from muliao.runtime.shell import kill_group, redact, spawn_bash
from muliao.runtime.tasks import Clock, TaskQueue
from muliao.vault import Vault

log = logging.getLogger(__name__)

JOBS_DIR = "jobs"
_CHUNK = 65536
_TAIL_LINES = 30
_TAIL_BYTES = 16384


def job_source(job_id: int) -> str:
    """任务发出的事件的来源，也用来判断它是否已有事件在队列里等着。"""
    return f"job:{job_id}"


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
    supervisor: asyncio.Task[None] | None = field(default=None)


class JobRunner:
    def __init__(
        self,
        db: Database,
        queue: TaskQueue,
        vault: Vault,
        workspace_of: Callable[[str], Path],
        check_seconds: float,
        clock: Clock = utcnow,
    ):
        self._db = db
        self._queue = queue
        self._vault = vault
        self._workspace_of = workspace_of
        self.check_seconds = check_seconds
        self._clock = clock
        self._running: dict[int, _Running] = {}

    async def start(
        self,
        *,
        session_id: str | None,
        agent_id: str,
        command: str,
        secrets: list[str],
        reply_to: ReplyTo | None,
    ) -> Job:
        if session_id is None or reply_to is None:
            raise InvalidInput(
                "background jobs need a conversation to report back to; run the command in the foreground"
            )
        if reply_to == ReplyTo.AGENT:
            raise InvalidInput(
                "background jobs are not available in a private thread: the asking agent is waiting for this answer; "
                "run the command in the foreground"
            )
        # vault 自己开事务，不能放进下面的事务
        values = await self._vault.resolve_many(agent_id, secrets)
        async with self._db.transaction() as s:
            session = await s.get(Session, session_id)
            if session is None:
                raise NotFound(f"session {session_id!r} not found")
            if session.private:
                raise Conflict(f"session {session_id} is a private thread; it cannot run background jobs")
            job = Job(
                session_id=session_id,
                agent_id=agent_id,
                command=command,
                secrets=list(secrets),
                reply_to=reply_to,
                log_path="",
                started_at=self._clock(),
            )
            s.add(job)
            await s.flush()
            job.log_path = f"{JOBS_DIR}/{job.id}.log"
        workspace = self._workspace_of(agent_id)
        log_file = workspace / job.log_path
        try:
            await asyncio.to_thread(log_file.parent.mkdir, parents=True, exist_ok=True)
            proc = await spawn_bash(command, cwd=workspace, secrets=values, merge_stderr=True)
        except BaseException:
            # 进程没起来：这个任务不存在
            async with self._db.transaction() as s:
                await s.delete(await s.get(Job, job.id))
            raise
        async with self._db.transaction() as s:
            row = await s.get(Job, job.id)
            if row is None:
                raise RuntimeError(f"job {job.id} vanished right after it was created")
            row.pid = job.pid = proc.pid
        handle = _Running(proc)
        self._running[job.id] = handle
        handle.supervisor = asyncio.create_task(
            self._supervise(job.id, handle, log_file, values), name=f"job-{job.id}"
        )
        handle.supervisor.add_done_callback(_log_supervisor_failure)
        log.info(
            "background job started", extra={"job_id": job.id, "session_id": session_id, "pid": proc.pid}
        )
        return job

    async def _supervise(
        self, job_id: int, handle: _Running, log_file: Path, secrets: Mapping[str, str]
    ) -> None:
        proc = handle.proc
        assert proc.stdout is not None
        try:
            with log_file.open("ab") as out:
                pending = b""
                while chunk := await proc.stdout.read(_CHUNK):
                    pending += chunk
                    # 按行脱敏：secret 不会被截在两块之间（超长的一行到上限也写出去）
                    complete, sep, pending = pending.rpartition(b"\n")
                    if sep:
                        out.write(redact((complete + sep).decode(errors="replace"), secrets).encode())
                    if len(pending) > _CHUNK:
                        out.write(redact(pending.decode(errors="replace"), secrets).encode())
                        pending = b""
                    out.flush()
                if pending:
                    out.write(redact(pending.decode(errors="replace"), secrets).encode())
            exit_code = await proc.wait()
        except asyncio.CancelledError:
            # 关停：进程随服务一起结束，下次启动时 recover 记为被中断并通知
            kill_group(proc.pid)
            raise
        except Exception:
            # 读日志、写文件意外失败：进程不能没人管地留着；任务记为被中断，失败原因进日志，agent 收到事件
            log.exception("background job supervision failed", extra={"job_id": job_id})
            kill_group(proc.pid)
            await self._finish(
                job_id, JobStatus.INTERRUPTED, None, "its output could not be recorded (see server log)"
            )
            return
        finally:
            self._running.pop(job_id, None)
        await self._finish(job_id, JobStatus.KILLED if handle.killed else JobStatus.EXITED, exit_code, None)

    async def _finish(
        self, job_id: int, status: JobStatus, exit_code: int | None, reason: str | None
    ) -> None:
        async with self._db.transaction() as s:
            job = await s.get(Job, job_id)
            if job is None:
                # 有后台任务在跑的会话不能删除（conversation.delete_sessions）
                raise RuntimeError(f"job {job_id} vanished while it was running")
            now = self._clock()
            job.status, job.exit_code, job.finished_at = status, exit_code, now
            text = _final_text(job, now, tail(self._workspace_of(job.agent_id) / job.log_path), reason)
            await post_in(
                s, self._queue, job.session_id, source=job_source(job_id), content=text, reply_to=job.reply_to
            )
        log.info(
            "background job finished", extra={"job_id": job_id, "status": status, "exit_code": exit_code}
        )

    async def kill(self, job_id: int, agent_id: str) -> None:
        """结束这个 agent 的一个运行中的任务；结果（KILLED）随后作为事件到达。"""
        async with self._db.transaction() as s:
            job = await _own(s, job_id, agent_id)
            if job.status != JobStatus.RUNNING:
                raise Conflict(f"job {job_id} is not running (it {job.status})")
        handle = self._running.get(job_id)
        if handle is None:
            raise RuntimeError(f"job {job_id} is recorded as running but not supervised by this process")
        handle.killed = True
        kill_group(handle.proc.pid)
        log.info("background job kill requested", extra={"job_id": job_id})

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
        """给每个运行中的任务发一个检查事件；它上一条事件还在队列里等着时不再发（不堆积）。返回发出的个数。"""
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
                waiting = await s.scalar(
                    select(
                        select(SessionEvent.id)
                        .where(SessionEvent.source == job_source(job.id), SessionEvent.consumed_by.is_(None))
                        .exists()
                    )
                )
                if waiting:
                    continue
                text = _check_text(job, self._clock(), tails[job.id])
                await post_in(
                    s,
                    self._queue,
                    job.session_id,
                    source=job_source(job.id),
                    content=text,
                    reply_to=ReplyTo.NONE,
                )
                posted += 1
        return posted

    async def recover(self) -> None:
        """启动时（还没有任务在跑）：上次运行留下的「运行中」任务，进程已随服务结束，记为被中断并通知 agent。"""
        async with self._db.transaction() as s:
            stale = list((await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING))).all())
            now = self._clock()
            for job in stale:
                job.status, job.finished_at = JobStatus.INTERRUPTED, now
                text = _final_text(
                    job, now, tail(self._workspace_of(job.agent_id) / job.log_path), "the service restarted"
                )
                await post_in(
                    s,
                    self._queue,
                    job.session_id,
                    source=job_source(job.id),
                    content=text,
                    reply_to=job.reply_to,
                )
        if stale:
            log.warning("background jobs interrupted by a restart", extra={"job_ids": [j.id for j in stale]})

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.check_seconds)
            await self.check()

    async def stop(self) -> None:
        """关停：杀掉所有后台任务的进程组（下次启动 recover 记为被中断）。"""
        for handle in list(self._running.values()):
            kill_group(handle.proc.pid)
            if handle.supervisor is not None:
                handle.supervisor.cancel()
        await asyncio.gather(
            *(h.supervisor for h in self._running.values() if h.supervisor), return_exceptions=True
        )


def _log_supervisor_failure(task: asyncio.Task[None]) -> None:
    """监督任务里的异常没有人 await：记下来（任务停在运行中，下次启动由 recover 记为被中断并通知 agent）。"""
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("background job supervisor crashed: %r", exc, exc_info=exc, extra={"task": task.get_name()})


async def _own(s, job_id: int, agent_id: str) -> Job:  # noqa: ANN001
    job = await s.get(Job, job_id)
    if job is None or job.agent_id != agent_id:
        raise NotFound(f"job {job_id} not found")
    return job


def _final_text(job: Job, now: datetime, last: str, reason: str | None) -> str:
    took = _duration(job.started_at, now)
    if job.status == JobStatus.EXITED:
        head = f"Background job {job.id} finished with exit code {job.exit_code} after {took}."
    elif job.status == JobStatus.KILLED:
        head = f"Background job {job.id} was killed (job_kill) after {took}."
    else:
        head = f"Background job {job.id} was interrupted after {took}: {reason}. It may have partly run."
    return f"{head}\nCommand: {job.command}\n{_output(job, last)}"


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
