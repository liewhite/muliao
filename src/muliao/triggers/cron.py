"""cron 触发源：到点为指定 agent 新建 session 并投递 turn。

策略：
- 错过的触发（进程停机期间）只补一次，然后从当前时间算下一次。
- 不重叠：上一次触发的 session 仍有未完成 turn 时跳过本次，并记 info 日志。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.models import Agent, CronJob, DeliveryChannel, Origin
from muliao.runtime.sessions import SessionService
from muliao.runtime.tasks import Clock, TaskQueue, turn_lane

log = logging.getLogger(__name__)

_MAX_SLEEP_SECONDS = 60.0
_FAILURE_BACKOFF_SECONDS = 10.0


class CronService:
    def __init__(
        self, db: Database, sessions: SessionService, queue: TaskQueue, tz: ZoneInfo, clock: Clock = utcnow
    ):
        self._db = db
        self._sessions = sessions
        self._queue = queue
        self._tz = tz
        self._clock = clock
        self._changed = asyncio.Event()

    def next_run(self, schedule: str, after: datetime) -> datetime:
        if not croniter.is_valid(schedule):
            raise InvalidInput(f"invalid cron expression {schedule!r}")
        return croniter(schedule, after.astimezone(self._tz)).get_next(datetime)

    async def create(self, *, name: str, agent_name: str, schedule: str, prompt: str) -> CronJob:
        if not name.strip() or not prompt.strip():
            raise InvalidInput("cron job name and prompt must not be empty")
        job = CronJob(
            name=name.strip(),
            agent_name=agent_name,
            schedule=schedule,
            prompt=prompt,
            enabled=True,
            next_run_at=self.next_run(schedule, self._clock()),
        )
        try:
            async with self._db.transaction() as s:
                if await s.get(Agent, agent_name) is None:
                    raise NotFound(f"agent {agent_name!r} not found")
                s.add(job)
        except IntegrityError as e:
            raise Conflict(f"cron job {name!r} already exists") from e
        self._changed.set()
        log.info("cron job created", extra={"cron": job.name, "agent": agent_name, "schedule": schedule})
        return job

    async def update(self, job_id: int, *, schedule: str, prompt: str, enabled: bool) -> CronJob:
        next_run = self.next_run(schedule, self._clock())
        async with self._db.transaction() as s:
            job = await self._get(s, job_id)
            job.schedule, job.prompt, job.enabled, job.next_run_at = schedule, prompt, enabled, next_run
        self._changed.set()
        return job

    async def _get(self, s, job_id: int) -> CronJob:  # noqa: ANN001
        job = await s.get(CronJob, job_id)
        if job is None:
            raise NotFound(f"cron job {job_id} not found")
        return job

    async def get_by_name(self, name: str) -> CronJob:
        async with self._db.transaction() as s:
            job = await s.scalar(select(CronJob).where(CronJob.name == name))
            if job is None:
                raise NotFound(f"cron job {name!r} not found")
            return job

    async def list(self, agent_name: str | None = None) -> list[CronJob]:
        query = select(CronJob).order_by(CronJob.name)
        if agent_name is not None:
            query = query.where(CronJob.agent_name == agent_name)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def delete(self, job_id: int) -> None:
        async with self._db.transaction() as s:
            await s.delete(await self._get(s, job_id))
        self._changed.set()

    async def fire_due(self) -> int:
        now = self._clock()
        async with self._db.transaction() as s:
            due_ids = list(
                (await s.scalars(select(CronJob.id).where(CronJob.enabled, CronJob.next_run_at <= now))).all()
            )
        for job_id in due_ids:
            await self._fire(job_id, now)
        return len(due_ids)

    async def _fire(self, job_id: int, now: datetime) -> None:
        async with self._db.transaction() as s:
            job = await self._get(s, job_id)
            job.next_run_at = self.next_run(job.schedule, now)
            ctx = {"cron": job.name, "agent": job.agent_name}
            if job.last_session_id and await self._queue.lane_busy(s, turn_lane(job.last_session_id)):
                log.info("cron fire skipped: previous run still in progress", extra=ctx)
                return
            local = now.astimezone(self._tz).strftime("%Y-%m-%d %H:%M")
            session = await self._sessions.create_in(
                s,
                agent_name=job.agent_name,
                prompt=job.prompt,
                origin=Origin.CRON,
                origin_ref=str(job.id),
                delivery=DeliveryChannel.NONE,
                title=f"{job.name} · {local}",
            )
            job.last_run_at = now
            job.last_session_id = session.id
            log.info("cron fired", extra={**ctx, "session_id": session.id})

    async def _next_wake(self) -> float:
        async with self._db.transaction() as s:
            nxt = await s.scalar(
                select(CronJob.next_run_at).where(CronJob.enabled).order_by(CronJob.next_run_at)
            )
        if nxt is None:
            return _MAX_SLEEP_SECONDS
        return min(_MAX_SLEEP_SECONDS, max(0.0, (nxt - self._clock()).total_seconds()))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.fire_due()
            except Exception:
                # 失败的 job 不推进 next_run_at，下一轮会再次暴露；退避避免空转刷日志
                log.exception("cron scheduling pass failed, backing off")
                await asyncio.sleep(_FAILURE_BACKOFF_SECONDS)
                continue
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=await self._next_wake())
            except TimeoutError:
                pass
