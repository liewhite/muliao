"""流式触发：config = {"command": "<bash 命令>", "secrets": [...], "debounce_seconds": 5}。

平台托管一条常驻命令（如 Slack Socket Mode 监听），它的 stdout 每一行是一个事件：
- 命令在该 agent 的工作目录里用 bash -o pipefail 执行，环境与 run_shell、前置检查相同（platform/shell.py：最小环境
  变量、按 key 注入 secret、输出里的 secret 替换掉、自成进程组）。随服务启停；停用、删除、改配置时杀掉整个进程组，
  改了配置用新配置重新起。
- 退出（不论退出码：它本该一直运行）或起不来（secret 不可见）后按退避时间重启（1、2、4……秒，最多 5 分钟）。
  稳定运行一段时间（_STABLE_SECONDS）算恢复：清除错误、退避从头算。最近一次的原因记在 state["error"]（含
  stderr 的结尾），连续快速失败达到 _ALERT_AFTER 次时通知用户一次（偶尔退出重连是常态，不打扰）。
- stderr 追加写进工作目录下的 trigger-logs/<名称>.stderr.log（超过 1 MiB 轮换一份 .1），agent 可以自己读。
- 事件按 debounce 窗口合并：第一行到达后再等 debounce_seconds，期间到达的一起成为一次触发，事件内容是这些行
  （以换行分隔）。上一次触发的会话还没做完（previous_fire_state_in，与定时触发同一个定义）时事件排队、不重叠，
  它结束后合并投递；挡路的会话在等待（可能永远等不到，如没人回答的提问）时通知用户一次。排队的事件超过事件上限
  时不再等，先投一批（会与挡路的会话重叠：宁可重叠也不丢事件）。单行超过事件上限的丢弃并记日志。
- 排队的事件只在内存里：服务重启、改配置、停用时丢失。命令自己做断线重连、去重与游标，重要的来源要另加轮询兜底
  （公共 skill stream-listener）。
- 每次触发走 TriggerService.fire（新开会话；有前置检查时先跑，事件内容作为它的 stdin）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from muliao.errors import Conflict, DomainError, NotFound
from muliao.notifications.events import Notify
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Messaging
from muliao.platform.shell import DRAIN_SECONDS, kill_group, redact, spawn_bash
from muliao.platform.tasks import Clock
from muliao.sessions.api import SessionActivity, WorkState
from muliao.triggers.precheck import PrecheckFailed
from muliao.triggers.service import (
    TriggerService,
    parse_config,
    previous_fire_state_in,
    set_state,
)
from muliao.triggers.tables import Trigger
from muliao.vault.api import Vault

log = logging.getLogger(__name__)

KIND = "stream"

# 稳定运行这么久算恢复：清除错误，退避与连续失败计数从头算
_STABLE_SECONDS = 60.0
# 连续快速失败这么多次才通知用户（每一轮连续失败只通知一次）
_ALERT_AFTER = 3
_MAX_BACKOFF_SECONDS = 300.0
# 上一次触发的会话还没做完时，隔多久再看一次
_BUSY_POLL_SECONDS = 2.0
_STDERR_TAIL_CHARS = 2000
_LOG_MAX_BYTES = 1024 * 1024
LOG_DIR = "trigger-logs"


class StreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=4000)
    secrets: list[str] = []
    debounce_seconds: float = Field(default=5.0, ge=0, le=3600)


class StreamStatus(BaseModel):
    # 当前这个进程的启动时间；没有在运行（停用，或退出后在等重启）时为 null
    running_since: datetime | None
    # 最近一次退出或起不来的原因（含 stderr 结尾）；稳定运行一段时间后清除
    error: str | None
    # 连续快速失败的次数
    failures: int
    # stderr 写到哪个文件（相对该 agent 的工作目录）
    stderr_log: str


def stderr_log(name: str) -> str:
    return f"{LOG_DIR}/{name}.stderr.log"


class StreamKind:
    name = KIND
    description = (
        "Runs a long-lived command hosted by the platform; every line it prints to stdout is an event. config: "
        '{"command": "<bash command>", "secrets": ["VAULT_KEY", ...], "debounce_seconds": 5}. The command runs in '
        "the agent's workspace with the secrets as env vars; it is restarted with backoff whenever it exits, and "
        "killed when the trigger is disabled, deleted or reconfigured. Lines arriving within debounce_seconds of "
        "the first one fire together (one conversation, the lines are the event); while the conversation of the "
        "previous firing is still busy, events queue and are delivered together once it is done. stderr goes to "
        f"{LOG_DIR}/<name>.stderr.log in the workspace. Queued events are lost on a restart, so the command handles "
        "reconnects, dedup and cursors itself; read public skill stream-listener before writing one."
    )

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        parsed = parse_config(KIND, StreamConfig, config)
        # 新配置的进程从头算：之前的错误与失败计数不带过来
        return parsed.model_dump(), {"running_since": None, "error": None, "failures": 0}

    def status(self, trigger: Trigger) -> StreamStatus:
        state = trigger.state
        return StreamStatus(
            # 停用时进程已被杀掉（关停时来不及写回，所以按启用状态推导，而不是信存下的值）
            running_since=state.get("running_since") if trigger.enabled else None,
            error=state.get("error"),
            failures=state.get("failures", 0),
            stderr_log=stderr_log(trigger.name),
        )


def _backoff(failures: int) -> float:
    return min(_MAX_BACKOFF_SECONDS, 2.0 ** (failures - 1))


@dataclass(frozen=True)
class _Spec:
    """一个托管进程由什么决定：其中任何一项变了，就杀掉旧进程按新的重起。"""

    trigger_id: int
    name: str
    agent_id: str
    config_json: str

    @property
    def config(self) -> StreamConfig:
        return StreamConfig.model_validate_json(self.config_json)

    @classmethod
    def of(cls, t: Trigger) -> _Spec:
        return cls(t.id, t.name, t.agent_id, json.dumps(t.config, sort_keys=True))


@dataclass
class _Pending:
    """排队等投递的事件（只在内存里）。"""

    lines: list[str] = field(default_factory=list)
    size: int = 0
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, line: str) -> None:
        self.lines.append(line)
        self.size += len(line.encode()) + 1
        self.arrived.set()

    def take(self, max_bytes: int) -> list[str]:
        """取出不超过 max_bytes 的一批（每行都不超过上限，所以至少取出一行）。"""
        batch, size = [], 0
        for line in self.lines:
            n = len(line.encode()) + 1
            if batch and size + n > max_bytes:
                break
            batch.append(line)
            size += n
        del self.lines[: len(batch)]
        self.size -= size
        if not self.lines:
            self.arrived.clear()
        return batch


class StreamRunner:
    """流式触发的激活机制：为每个启用的流式触发器托管一个进程，把它的输出行合并成触发。"""

    def __init__(
        self,
        db: Database,
        triggers: TriggerService,
        activity: SessionActivity,
        vault: Vault,
        workspace_of: Callable[[str], Path],
        messaging: Messaging,
        max_bytes: int,
        clock: Clock = utcnow,
        backoff: Callable[[int], float] = _backoff,
        stable_seconds: float = _STABLE_SECONDS,
        busy_poll_seconds: float = _BUSY_POLL_SECONDS,
    ):
        self._db = db
        self._triggers = triggers
        self._activity = activity
        self._vault = vault
        self._workspace_of = workspace_of
        self._messaging = messaging
        self._max_bytes = max_bytes
        self._clock = clock
        self._backoff = backoff
        self._stable_seconds = stable_seconds
        self._busy_poll_seconds = busy_poll_seconds
        self._changed = triggers.watch()

    async def run_forever(self) -> None:
        """按触发器的变化对账：该运行的起起来，停用、删除或改了配置的取消（取消即杀掉进程组）。"""
        running: dict[int, tuple[_Spec, anyio.CancelScope]] = {}
        async with anyio.create_task_group() as group:
            while True:
                self._changed.clear()
                wanted = {t.id: _Spec.of(t) for t in await self._triggers.list(kind=KIND, enabled_only=True)}
                for trigger_id, (spec, scope) in list(running.items()):
                    if wanted.get(trigger_id) != spec:
                        scope.cancel()
                        del running[trigger_id]
                        log.info("stream stopped", extra={"trigger": spec.name})
                for trigger_id, spec in wanted.items():
                    if trigger_id not in running:
                        scope = anyio.CancelScope()
                        running[trigger_id] = (spec, scope)
                        group.start_soon(self._supervise, spec, scope)
                await self._changed.wait()

    async def _supervise(self, spec: _Spec, scope: anyio.CancelScope) -> None:
        try:
            config = spec.config
        except ValidationError as e:
            # 配置只经 configure 写入；存下的坏了是数据问题，只停用这一个
            await self._triggers.disable(spec.trigger_id, f"invalid stored stream config: {e}")
            return
        with scope:
            pending = _Pending()
            async with anyio.create_task_group() as group:
                group.start_soon(self._deliver_loop, spec, config, pending)
                await self._process_loop(spec, config, pending)

    # ---- 进程 ----

    async def _process_loop(self, spec: _Spec, config: StreamConfig, pending: _Pending) -> None:
        failures = 0
        while True:
            stable = anyio.Event()
            error = await self._run_once(spec, config, pending, stable)
            # 稳定运行过的不算连续失败，从最短的退避重来
            if stable.is_set():
                failures = 0
            failures += 1
            log.warning("stream exited: %s", error, extra={"trigger": spec.name, "failures": failures})
            await self._record(
                spec,
                alert=self._alert(spec, error) if failures == _ALERT_AFTER else None,
                running_since=None,
                error=error,
                failures=failures,
            )
            await anyio.sleep(self._backoff(failures))

    async def _run_once(
        self, spec: _Spec, config: StreamConfig, pending: _Pending, stable: anyio.Event
    ) -> str:
        """起一次进程并跑到它退出，返回退出的原因。"""
        try:
            secrets = await self._vault.resolve_many(spec.agent_id, config.secrets)
        except NotFound as e:
            return f"stream secrets unavailable: {e}"
        cwd = self._workspace_of(spec.agent_id)
        proc = await spawn_bash(config.command, cwd=cwd, secrets=secrets, pipefail=True)
        log.info("stream started", extra={"trigger": spec.name, "pid": proc.pid})
        await self._record(spec, running_since=self._clock().isoformat())
        tail: list[str] = []
        try:
            async with anyio.create_task_group() as watchers:
                watchers.start_soon(self._mark_stable, spec, stable)
                async with anyio.create_task_group() as readers:
                    readers.start_soon(self._read_events, spec, proc.stdout, secrets, pending)
                    readers.start_soon(
                        self._read_stderr, cwd / stderr_log(spec.name), proc.stderr, secrets, tail
                    )
                    code = await proc.wait()
                    # 进程退出了，脱离不了进程组的后代可能还持有管道：杀掉整组，剩下的输出限时读完
                    kill_group(proc.pid)
                    readers.cancel_scope.deadline = anyio.current_time() + DRAIN_SECONDS
                watchers.cancel_scope.cancel()
        finally:
            # 被取消（停用、删除、改配置、关停）时不留下还在跑的命令，并回收它（不留僵尸进程与没关的管道）
            kill_group(proc.pid)
            with anyio.CancelScope(shield=True), anyio.move_on_after(DRAIN_SECONDS):
                await proc.wait()
        return f"stream command exited with {code}: {''.join(tail)[-_STDERR_TAIL_CHARS:].strip()}"

    async def _mark_stable(self, spec: _Spec, stable: anyio.Event) -> None:
        await anyio.sleep(self._stable_seconds)
        stable.set()
        await self._record(spec, error=None, failures=0)

    async def _read_events(
        self, spec: _Spec, stream: asyncio.StreamReader | None, secrets: dict[str, str], pending: _Pending
    ) -> None:
        assert stream is not None
        buf = bytearray()
        # 正在丢弃一个超长行的剩余部分（直到下一个换行）
        skipping = False
        while chunk := await stream.read(65536):
            buf += chunk
            while (end := buf.find(b"\n")) >= 0:
                raw = bytes(buf[:end])
                del buf[: end + 1]
                if skipping:
                    skipping = False
                    continue
                self._emit(spec, raw, secrets, pending)
            if len(buf) > self._max_bytes:
                log.warning(
                    "stream line over the %d-byte event limit dropped",
                    self._max_bytes,
                    extra={"trigger": spec.name},
                )
                buf.clear()
                skipping = True
        if buf and not skipping:
            self._emit(spec, bytes(buf), secrets, pending)

    def _emit(self, spec: _Spec, raw: bytes, secrets: dict[str, str], pending: _Pending) -> None:
        line = redact(raw.decode(errors="replace"), secrets).rstrip("\r")
        if not line.strip():
            return
        if len(line.encode()) + 1 > self._max_bytes:
            log.warning("stream line over the event limit dropped", extra={"trigger": spec.name})
            return
        pending.add(line)

    async def _read_stderr(
        self, path: Path, stream: asyncio.StreamReader | None, secrets: dict[str, str], tail: list[str]
    ) -> None:
        assert stream is not None
        while chunk := await stream.read(65536):
            text = redact(chunk.decode(errors="replace"), secrets)
            tail.append(text)
            if sum(map(len, tail)) > 2 * _STDERR_TAIL_CHARS:
                joined = "".join(tail)[-_STDERR_TAIL_CHARS:]
                tail[:] = [joined]
            await anyio.to_thread.run_sync(_append_log, path, text)

    # ---- 投递 ----

    async def _deliver_loop(self, spec: _Spec, config: StreamConfig, pending: _Pending) -> None:
        while True:
            await pending.arrived.wait()
            await anyio.sleep(config.debounce_seconds)
            await self._wait_previous(spec, pending)
            batch = pending.take(self._max_bytes)
            try:
                await self._triggers.fire(spec.trigger_id, "\n".join(batch))
            except PrecheckFailed:
                # 已记在触发器上（并在从正常变为出错时通知了用户）；这批事件丢弃
                log.warning("stream batch dropped by failing precheck", extra={"trigger": spec.name})
            except (Conflict, NotFound) as e:
                # 刚被停用或删除：对账会取消这个循环
                log.info("stream fire skipped: %s", e, extra={"trigger": spec.name})
            except DomainError as e:
                await self._triggers.disable(spec.trigger_id, f"{type(e).__name__}: {e}")

    async def _wait_previous(self, spec: _Spec, pending: _Pending) -> None:
        """上一次触发的会话还没做完时事件排队；排队的超过事件上限就不再等。在等待的会话挡路时通知用户一次。"""
        notified: str | None = None
        while pending.size <= self._max_bytes:
            async with self._db.transaction() as s:
                trigger = await s.get(Trigger, spec.trigger_id)
                if trigger is None:
                    return
                state = await previous_fire_state_in(s, self._activity, trigger)
                if state == WorkState.IDLE:
                    return
                if state == WorkState.WAITING and notified != trigger.last_session_id:
                    notified = trigger.last_session_id
                    self._messaging.send_in(s, self._waiting_notice(trigger, len(pending.lines)))
            await anyio.sleep(self._busy_poll_seconds)

    @staticmethod
    def _waiting_notice(trigger: Trigger, queued: int) -> Notify:
        return Notify(
            agent_id=trigger.agent_id,
            title=f"流式触发器 {trigger.name} 的事件在排队",
            body=(
                f"上一次触发的会话在等待（后台任务、委派或你的回答），它结束前新事件都排着，现在有 {queued} 条。"
                "点开看它在等什么。"
            ),
            session_id=trigger.last_session_id,
            issue_id=None,
        )

    # ---- 状态 ----

    @staticmethod
    def _alert(spec: _Spec, error: str) -> Notify:
        return Notify(
            agent_id=spec.agent_id,
            title=f"流式触发器 {spec.name} 的命令反复退出",
            body=f"连续 {_ALERT_AFTER} 次很快退出，仍在按退避重启。最近一次：{error}"[:500],
            session_id=None,
            issue_id=None,
        )

    async def _record(self, spec: _Spec, alert: Notify | None = None, **values: Any) -> None:
        """把进程的运行状态写回触发器。只写给还是这份配置、仍启用的触发器：被取消的旧进程不覆盖新进程的状态。"""
        async with self._db.transaction() as s:
            trigger = await s.get(Trigger, spec.trigger_id)
            if trigger is None or not trigger.enabled or _Spec.of(trigger) != spec:
                return
            set_state(trigger, **values)
            if alert is not None:
                self._messaging.send_in(s, alert)


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
