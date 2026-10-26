"""流式触发：config = {"command": "<bash 命令>", "secrets": [...], "debounce_seconds": 5}。

平台托管一条常驻命令（如 Slack Socket Mode 监听），它的 stdout 每一行是一个事件：
- 命令在该 agent 的工作目录里用 bash -o pipefail 执行，环境与 run_shell、前置检查相同（platform/shell.py：最小环境
  变量、按 key 注入 secret、输出里的 secret 替换掉、自成进程组）。随服务启停；停用、删除、改配置时杀掉整个进程组，
  改了配置用新配置重新起。
- 退出（不论退出码：它本该一直运行）或起不来（secret 不可见、日志写不进去等）都算一次失败，按退避时间重启（1、2、
  4……秒，最多 5 分钟）。稳定运行一段时间（_STABLE_SECONDS）算恢复：清除错误、退避从头算。最近一次的原因记在
  state["error"]（含 stderr 日志的结尾），连续快速失败达到 _ALERT_AFTER 次时通知用户一次（偶尔退出重连是常态，
  不打扰）。进程的运行状态（running_since、error、failures）只由这里维护，每个托管进程开始时清零：服务重启、换配置
  都从头算。
- stderr 逐块脱敏后追加进工作目录下的 trigger-logs/<名称>.stderr.log（与后台任务共用 platform.output；每次启动前
  超过 1 MiB 就轮换一份 .1），agent 可以自己读。
- 事件按 debounce 窗口合并：第一行到达后再等 debounce_seconds，期间到达的一起成为一次触发，事件内容是这些行
  （以换行分隔）。上一次触发的会话还没做完（previous_fire_state_in，与定时触发同一个定义）时事件排队、不重叠，
  它结束后合并投递；挡路的会话在等待（可能永远等不到，如没人回答的提问）时通知用户一次（这一份托管进程里每个挡路
  会话一次）。排队的事件以事件上限为界：排满了就不再读命令的输出，管道写满后命令自己阻塞——不丢、不重叠，内存与
  并行会话都有界。单行超过事件上限的丢弃并记日志。
- 排队的事件只在内存里：服务重启、改配置、停用时丢失；一批开始触发后不再重试（至多一次：前置检查失败、
  触发意外失败都丢弃这一批）；命令退出时队列正满，管道里还没读的输出只限时读、读不完的丢弃。命令自己
  做断线重连、去重与游标，重要的来源要另加轮询兜底（skill stream-listener）。
- 每次触发走 TriggerService.fire_activated（新开会话；有前置检查时先跑，事件内容作为它的 stdin）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from muliao.errors import NotFound
from muliao.notifications.events import Notify
from muliao.platform.db import Database, utcnow
from muliao.platform.messaging import Messaging
from muliao.platform.output import CHUNK, record_output, tail, wait_exit_code
from muliao.platform.shell import DRAIN_SECONDS, kill_group, redact, spawn_bash
from muliao.platform.tasks import Clock
from muliao.sessions.api import SessionActivity, WorkState
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
_ERROR_TAIL_CHARS = 2000
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
        "reconnects, dedup and cursors itself; read skill stream-listener before writing one."
    )

    def configure(self, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        # 运行状态不由配置推导：它由托管进程维护（StreamRunner），每个托管进程开始时清零
        return parse_config(KIND, StreamConfig, config).model_dump(), {}

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
    """排队等投递的事件（只在内存里），总量不超过事件上限：排满时读取方等着，命令的输出留在管道里。"""

    max_bytes: int
    lines: list[str] = field(default_factory=list)
    size: int = 0
    # 这一批第一行到达的时间（anyio 时钟），debounce 从它算起
    first_at: float = 0.0
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    async def put(self, line: str) -> None:
        n = len(line.encode()) + 1
        while self.lines and self.size + n > self.max_bytes:
            self.changed.clear()
            await self.changed.wait()
        if not self.lines:
            self.first_at = anyio.current_time()
        self.lines.append(line)
        self.size += n
        self.changed.set()

    async def ready(self, debounce: float) -> None:
        """等到有事件、且第一行到达后过了 debounce 秒。"""
        while not self.lines:
            self.changed.clear()
            await self.changed.wait()
        await anyio.sleep(max(0.0, self.first_at + debounce - anyio.current_time()))

    def take(self) -> list[str]:
        batch, self.lines, self.size = self.lines, [], 0
        self.changed.set()
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
        """按触发器的变化对账：该运行的起起来，停用、删除或改了配置的取消（取消即杀掉进程组）。

        每个托管进程是一个独立的监督者，有自己的异常边界：一个触发器出意外不影响别的，也不让服务退出。"""
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
        with scope:
            try:
                config = spec.config
            except ValidationError as e:
                # 配置只经 configure 写入；存下的坏了是数据问题，只停用这一个
                await self._safely(
                    spec, self._triggers.disable(spec.trigger_id, f"invalid stored stream config: {e}")
                )
                return
            pending = _Pending(self._max_bytes)
            async with anyio.create_task_group() as group:
                group.start_soon(self._deliver_loop, spec, config, pending)
                await self._process_loop(spec, config, pending)

    # ---- 进程 ----

    async def _process_loop(self, spec: _Spec, config: StreamConfig, pending: _Pending) -> None:
        # 这份托管进程从头算：上一个（服务重启前、旧配置）留下的运行状态清掉
        await self._safely(spec, self._record(spec, running_since=None, error=None, failures=0))
        failures = 0
        while True:
            stable = anyio.Event()
            try:
                error = await self._run_once(spec, config, pending, stable)
            except Exception as e:
                # 契约之外的失败（日志写不进去、起不了进程、数据库暂时不可用……）：进程已在 _run_once 里杀掉，按一次
                # 失败重启，不让它穿出去拖垮别的触发器
                log.exception("stream supervision failed", extra={"trigger": spec.name})
                error = f"stream supervision failed: {type(e).__name__}: {e}"
            # 稳定运行过的不算连续失败，从最短的退避重来
            if stable.is_set():
                failures = 0
            failures += 1
            log.warning("stream exited: %s", error, extra={"trigger": spec.name, "failures": failures})
            await self._safely(
                spec,
                self._record(
                    spec,
                    alert=self._alert(spec, error) if failures == _ALERT_AFTER else None,
                    running_since=None,
                    error=error,
                    failures=failures,
                ),
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
        log_file = cwd / stderr_log(spec.name)
        await anyio.to_thread.run_sync(
            _prepare_log, log_file, f"--- started {self._clock().isoformat()} ---\n"
        )
        proc = await spawn_bash(config.command, cwd=cwd, secrets=secrets, pipefail=True)
        # spawn 之后的任何一步（包括在这里被取消）都要杀掉进程组，所以紧接着进 try
        try:
            log.info("stream started", extra={"trigger": spec.name, "pid": proc.pid})
            await self._safely(spec, self._record(spec, running_since=self._clock().isoformat()))
            async with anyio.create_task_group() as watchers:
                watchers.start_soon(self._mark_stable, spec, stable)
                async with anyio.create_task_group() as readers:
                    assert proc.stdout is not None and proc.stderr is not None
                    readers.start_soon(self._read_events, spec, proc.stdout, secrets, pending)
                    readers.start_soon(record_output, proc.stderr, log_file, secrets)
                    # 以进程退出为准（Process.wait 要等管道全部关闭，后代占着管道就一直等）
                    code = await wait_exit_code(proc)
                    # 命令退出了，它留下的后代不能跟着下一次启动越积越多：杀掉整组，剩下的输出限时读完。
                    # 组长已被回收，但组里还有成员时这个进程组 id 不会被复用（POSIX）；没有成员时只会 ESRCH
                    kill_group(proc.pid)
                    readers.cancel_scope.deadline = anyio.current_time() + DRAIN_SECONDS
                watchers.cancel_scope.cancel()
        finally:
            # 被取消（停用、删除、改配置、关停）或意外失败时，不留下还在跑的命令
            kill_group(proc.pid)
            if proc.returncode is None:
                with anyio.CancelScope(shield=True), anyio.move_on_after(DRAIN_SECONDS):
                    await wait_exit_code(proc)
        stderr_tail = (await anyio.to_thread.run_sync(tail, log_file))[-_ERROR_TAIL_CHARS:].strip()
        return f"stream command exited with {code}: {stderr_tail}"

    async def _mark_stable(self, spec: _Spec, stable: anyio.Event) -> None:
        await anyio.sleep(self._stable_seconds)
        stable.set()
        await self._safely(spec, self._record(spec, error=None, failures=0))

    async def _read_events(
        self, spec: _Spec, stream: asyncio.StreamReader, secrets: Mapping[str, str], pending: _Pending
    ) -> None:
        buf = bytearray()
        # 正在丢弃一个超长行的剩余部分（直到下一个换行）
        skipping = False
        while chunk := await stream.read(CHUNK):
            buf += chunk
            while (end := buf.find(b"\n")) >= 0:
                raw = bytes(buf[:end])
                del buf[: end + 1]
                if skipping:
                    skipping = False
                    continue
                await self._emit(spec, raw, secrets, pending)
            if len(buf) > self._max_bytes:
                log.warning("stream line over the event limit dropped", extra={"trigger": spec.name})
                buf.clear()
                skipping = True
        if buf and not skipping:
            await self._emit(spec, bytes(buf), secrets, pending)

    async def _emit(self, spec: _Spec, raw: bytes, secrets: Mapping[str, str], pending: _Pending) -> None:
        line = redact(raw.decode(errors="replace").rstrip("\r"), secrets)
        if not line.strip():
            return
        if len(line.encode()) + 1 > self._max_bytes:
            log.warning("stream line over the event limit dropped", extra={"trigger": spec.name})
            return
        await pending.put(line)

    # ---- 投递 ----

    async def _deliver_loop(self, spec: _Spec, config: StreamConfig, pending: _Pending) -> None:
        # 已为哪个挡路的会话通知过用户（这份托管进程内每个挡路会话只通知一次）
        notified: list[str] = []
        while True:
            await pending.ready(config.debounce_seconds)
            try:
                await self._wait_previous(spec, len(pending.lines), notified)
            except Exception:
                # 契约之外的失败（如数据库暂时不可用）：事件留在队列里，稍后再试，不拖垮进程的监督
                log.exception(
                    "stream delivery could not check the previous session", extra={"trigger": spec.name}
                )
                await anyio.sleep(self._busy_poll_seconds)
                continue
            batch = pending.take()
            try:
                await self._triggers.fire_activated(spec.trigger_id, "\n".join(batch))
            except Exception:
                # 一旦开始触发就不重试这一批（至多一次）：fire 可能已经提交了投递才抛出，重试会重复触发
                log.exception(
                    "stream delivery failed, batch dropped", extra={"trigger": spec.name, "lines": len(batch)}
                )
                await anyio.sleep(self._busy_poll_seconds)

    async def _wait_previous(self, spec: _Spec, queued: int, notified: list[str]) -> None:
        """上一次触发的会话还没做完时等着（事件继续排队，排满后命令阻塞）；挡路的会话在等待时通知用户一次。"""
        while True:
            async with self._db.transaction() as s:
                trigger = await s.get(Trigger, spec.trigger_id)
                if trigger is None:
                    return
                state = await previous_fire_state_in(s, self._activity, trigger)
                if state == WorkState.IDLE:
                    return
                blocking = trigger.last_session_id
                if state == WorkState.WAITING and blocking is not None and blocking not in notified:
                    notified[:] = [blocking]
                    self._messaging.send_in(s, self._waiting_notice(trigger, queued))
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

    @staticmethod
    async def _safely(spec: _Spec, write: Awaitable[None]) -> None:
        """状态写不进去（数据库暂时不可用）不影响进程的监督：记日志，下次再写。"""
        try:
            await write
        except Exception:
            log.exception("stream state could not be recorded", extra={"trigger": spec.name})


def _prepare_log(path: Path, header: str) -> None:
    """每次启动前：建目录，超过上限就轮换一份 .1，再写一行分隔（agent 读日志时分得清是哪一次）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))
    with path.open("a", encoding="utf-8") as f:
        f.write(header)
