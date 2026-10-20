"""后台任务：agent 在后台跑的 shell 命令，会话拥有的异步工作的一种（runtime.work）。做法同 Claude Code 的后台 bash：
不占住运行、不设时限，结束时往发起它的会话的信箱投事件信件（runtime.mailbox），运行期间每隔一段时间发一个检查事件，
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.models import Job, JobStatus, MailKind, Session
from muliao.runtime.mailbox import post_in
from muliao.runtime.shell import DRAIN_SECONDS, kill_group, redact, spawn_bash
from muliao.runtime.tasks import Clock, TaskQueue
from muliao.runtime.work import post_check_in
from muliao.vault import Vault

log = logging.getLogger(__name__)

JOBS_DIR = "jobs"
_CHUNK = 65536
_TAIL_LINES = 30
_TAIL_BYTES = 16384
_EXIT_POLL_SECONDS = 0.2


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
    # 随会话停止（stop_session）：监督者照常记下结局，但不再投信（会话的信箱刚被清空）
    halted: bool = False
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
        awaited: bool,
    ) -> Job:
        """awaited：发起它的那次运行里用户是否在等回答（完成通知随它推送）。"""
        if session_id is None:
            raise InvalidInput(
                "background jobs need a conversation to report back to; run the command in the foreground"
            )
        # vault 自己开事务，不能放进下面的事务
        values = await self._vault.resolve_many(agent_id, secrets)
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
        # 进程一起来就有人管：先登记、起监督者，再记 pid
        handle = _Running(proc)
        self._running[job.id] = handle
        handle.supervisor = asyncio.create_task(
            self._supervise(job.id, handle, log_file, values), name=f"job-{job.id}"
        )
        handle.supervisor.add_done_callback(_log_supervisor_failure)
        try:
            async with self._db.transaction() as s:
                row = await s.get(Job, job.id)
                if row is None:
                    raise RuntimeError(f"job {job.id} vanished right after it was created")
                row.pid = job.pid = proc.pid
        except BaseException:
            # 记录写不进去，或这次运行恰好被停止：不能留下没有记录、没人能结束的进程（取消监督者会杀掉进程组）
            handle.supervisor.cancel()
            await asyncio.gather(handle.supervisor, return_exceptions=True)
            async with self._db.transaction() as s:
                await s.delete(await s.get(Job, job.id))
            raise
        log.info(
            "background job started", extra={"job_id": job.id, "session_id": session_id, "pid": proc.pid}
        )
        return job

    async def _supervise(
        self, job_id: int, handle: _Running, log_file: Path, secrets: Mapping[str, str]
    ) -> None:
        """看着进程直到结局写进库。「有人监督」（_running）一直保持到结局写完（成功、失败或被取消），
        对账才不会把正在记结局的任务当成没人管的。"""
        try:
            status, exit_code, note = await self._watch(job_id, handle, log_file, secrets)
            # 写结局失败（如数据库暂时不可用）时这一行停在 running；撤销登记后由 check 的对账收敛
            await self._finish(job_id, handle, status, exit_code, note)
        finally:
            self._running.pop(job_id, None)

    async def _watch(
        self, job_id: int, handle: _Running, log_file: Path, secrets: Mapping[str, str]
    ) -> tuple[JobStatus, int | None, str | None]:
        """以进程退出为准：退出后在有限时间内把管道里剩下的输出读完（脱离进程组的后代可能一直持有管道）。
        返回结局（状态、退出码、补充说明或被中断的原因）。"""
        proc = handle.proc
        recorder = asyncio.create_task(_record(proc, log_file, secrets))
        waiter = asyncio.create_task(_exit_code(proc))
        try:
            done, _ = await asyncio.wait({recorder, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if recorder in done:
                recorder.result()  # 记录输出失败：下面的 except 杀掉进程、记为被中断
            exit_code = await waiter
            ended = JobStatus.KILLED if handle.killed else JobStatus.EXITED
            _, unfinished = await asyncio.wait({recorder}, timeout=DRAIN_SECONDS)
            if not unfinished:
                recorder.result()
                return ended, exit_code, None
            recorder.cancel()
            await asyncio.gather(recorder, return_exceptions=True)
            return (
                ended,
                exit_code,
                "Processes it left running in the background still hold its output; their later output is not recorded.",
            )
        except asyncio.CancelledError:
            # 关停（stop 随后记为被中断）或启动时记录写不进去：进程不随服务退出，在这里杀掉
            kill_group(proc.pid)
            for task in (recorder, waiter):
                task.cancel()
            await asyncio.gather(recorder, waiter, return_exceptions=True)
            raise
        except Exception:
            # 读输出、写日志意外失败：进程不能没人管地留着；记为被中断，失败原因进日志，agent 收到事件
            log.exception("background job supervision failed", extra={"job_id": job_id})
            kill_group(proc.pid)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            return JobStatus.INTERRUPTED, None, "its output could not be recorded (see server log)"

    async def _finish(
        self, job_id: int, handle: _Running, status: JobStatus, exit_code: int | None, note: str | None
    ) -> None:
        async with self._db.transaction() as s:
            job = await s.get(Job, job_id)
            if job is None:
                # 有后台任务在跑的会话不能删除（conversation.delete_sessions）
                raise RuntimeError(f"job {job_id} vanished while it was running")
            if job.status != JobStatus.RUNNING:
                # 结局只记一次：监督者在写完之前一直登记着，对账与关停不会抢先记
                raise RuntimeError(f"job {job_id} already ended as {job.status}, cannot record {status}")
            now = self._clock()
            job.status, job.exit_code, job.finished_at = status, exit_code, now
            text = _final_text(job, now, tail(self._workspace_of(job.agent_id) / job.log_path), note)
            # 在事务里读：stop_session 设置它与这个事务之间没有交错（单线程，事务串行）
            if handle.halted:
                log.info("background job ended with its stopped session", extra={"job_id": job_id})
                return
            await post_in(
                s,
                self._queue,
                job.session_id,
                kind=MailKind.EVENT,
                source=job_source(job_id),
                content=text,
                awaited=job.awaited,
            )
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
        await post_in(
            s,
            self._queue,
            job.session_id,
            kind=MailKind.EVENT,
            source=job_source(job.id),
            content=text,
            awaited=job.awaited,
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

    async def active_in(self, s: AsyncSession, owner_session_id: str) -> bool:
        return bool(
            await s.scalar(
                select(
                    select(Job.id)
                    .where(Job.session_id == owner_session_id, Job.status == JobStatus.RUNNING)
                    .exists()
                )
            )
        )

    async def halt(self, owner_session_id: str) -> None:
        """用户停止会话：结束它正在跑的后台任务（同 job_kill），结局照常由监督者记下（KILLED），但不再给这个会话投信。
        调用方在清空信箱之前调用：标记在这里同步设上，之后监督者的结局事务一定看得到。"""
        async with self._db.transaction() as s:
            ids = list(
                (
                    await s.scalars(
                        select(Job.id).where(
                            Job.session_id == owner_session_id, Job.status == JobStatus.RUNNING
                        )
                    )
                ).all()
            )
        stopped = []
        for job_id in ids:
            handle = self._running.get(job_id)
            # 没有监督者的（监督意外结束）由 check 的对账收敛
            if handle is not None:
                handle.killed = handle.halted = True
                kill_group(handle.proc.pid)
                stopped.append(job_id)
        if stopped:
            log.info(
                "background jobs stopped with their session",
                extra={"session_id": owner_session_id, "job_ids": stopped},
            )

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

        先对账：记为运行中、却不在本进程监督之下的任务（记下结局时失败、监督意外结束），按 recover 的同一套逻辑
        记为被中断并告诉 agent。pid 还没记下的行是正在启动的任务（start 里登记之前），不算。"""
        async with self._db.transaction() as s:
            orphans = (
                await s.scalars(
                    select(Job).where(
                        Job.status == JobStatus.RUNNING,
                        Job.pid.is_not(None),
                        Job.id.not_in(list(self._running)),
                    )
                )
            ).all()
            for job in orphans:
                await self._interrupt_in(
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
                posted += await post_check_in(s, self._queue, job.session_id, job_source(job.id), text)
        return posted

    async def recover(self) -> None:
        """启动时（还没有任务在跑）：正常关停已把任务记为被中断，这里剩下的是服务意外退出（被强杀、崩溃）时还在跑的。
        进程自成进程组，不随服务退出，可能还活着：如实告诉 agent，不盲杀（pid 可能已被复用）。"""
        async with self._db.transaction() as s:
            stale = list((await s.scalars(select(Job).where(Job.status == JobStatus.RUNNING))).all())
            for job in stale:
                await self._interrupt_in(s, job, "the service stopped unexpectedly", may_be_alive=True)
        if stale:
            log.warning(
                "background jobs left by an unexpected stop", extra={"job_ids": [j.id for j in stale]}
            )

    async def stop(self) -> None:
        """正常关停：杀掉所有后台任务的进程组，记为被中断并告诉 agent（事件在重启后处理）。"""
        handles = dict(self._running)
        supervisors = [h.supervisor for h in handles.values() if h.supervisor is not None]
        for handle in handles.values():
            if handle.proc.returncode is not None:
                # 进程已经自己结束、正在记结局：给它时间写完，保留真实的退出码
                continue
            kill_group(handle.proc.pid)
            if handle.supervisor is not None:
                handle.supervisor.cancel()
        if supervisors:
            # 进程退出后监督者自己还要最多 DRAIN_SECONDS 读完剩余输出，再写结局
            _, unfinished = await asyncio.wait(supervisors, timeout=2 * DRAIN_SECONDS)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*supervisors, return_exceptions=True)
        if not handles:
            return
        async with self._db.transaction() as s:
            for job_id in handles:
                job = await s.get(Job, job_id)
                if job is not None and job.status == JobStatus.RUNNING:
                    await self._interrupt_in(s, job, "the service was stopped", may_be_alive=False)
        log.info("background jobs stopped with the service", extra={"job_ids": list(handles)})


def _log_supervisor_failure(task: asyncio.Task[None]) -> None:
    """监督任务里的异常没有人 await：记下来（任务停在运行中，check 的对账把它记为被中断并通知 agent）。"""
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("background job supervisor crashed: %r", exc, exc_info=exc, extra={"task": task.get_name()})


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
