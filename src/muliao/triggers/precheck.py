"""触发器的前置检查：触发前先跑一条便宜、确定性的命令，只有真有事才调用 agent（所有触发方式共用）。

约定（命令在该 agent 的工作目录里用 bash -o pipefail 执行，环境与 run_shell 相同，可按 key 注入 secret；stdin 是原始事件内容：
webhook 的请求体，定时与手动触发为空）。退出码 0 时按 fire_when 决定（decide）：
- output（默认）：stdout 非空就触发，stdout 作为事件内容交给 agent（替换原始事件，可以借此过滤、裁剪）；为空不触发。
  命令可以把游标（如上次读到的消息 id）存在工作目录里，自己做增量。
- changed：与上一次的输出逐字比较，变了才触发，交给 agent 的是差异加完整输出；命令只需打印当前状态，上一次的输出
  存在触发器上。第一次运行只记录基线、不触发（没有可比较的，叫 agent 只会白花一次模型调用）。
非 0 退出、超时、输出过大、secret 不可见：检查本身失败（PrecheckFailed），不触发，原因记在触发器上，并在从正常变为
出错时告警一次。不当作「没事」跳过——那会把脚本的故障变成静默的漏触发。

检查在触发器上的状态（precheck、precheck_output 基线、precheck_error、last_checked_at）只在这里迁移：换检查（set_precheck）
从新基线开始、清掉旧失败；检查期间触发器变了，结果作废（current_in）；成功（passed：判定是否触发并清掉失败）、失败（failed：
记下原因，从正常变为出错时要告警）。投信、告警的投递由 TriggerService 编排。
"""

import difflib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from muliao.agents.api import Vault
from muliao.errors import NotFound, ScriptFailed
from muliao.platform.shell import run_bash
from muliao.triggers.tables import Trigger

log = logging.getLogger(__name__)

_STDERR_TAIL_CHARS = 2000


class Precheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=4000, description="bash command run before each firing.")
    secrets: list[str] = Field(default=[], description="Vault keys exposed to the command as env vars.")
    fire_when: Literal["output", "changed"] = Field(
        default="output",
        description="'output': fire when stdout is non-empty. 'changed': fire when stdout differs from the last run "
        "(the first run only records it).",
    )


class PrecheckFailed(ScriptFailed):
    """前置检查本身失败。原因给用户与 agent 看（可能含脚本的 stderr），不交给 webhook 的外部调用方。"""


@dataclass(frozen=True)
class Decision:
    # 交给 agent 的事件内容；None 表示这次不触发
    event: str | None
    # 第一次 changed 检查：只记录基线
    baseline: bool = False


def decide(precheck: Precheck, output: str, previous: str | None, max_bytes: int) -> Decision:
    """由检查的输出与上一次的输出决定是否触发、交给 agent 什么（纯逻辑）。"""
    if precheck.fire_when == "output":
        return Decision(output if output.strip() else None)
    if previous is None:
        return Decision(None, baseline=True)
    if output == previous:
        return Decision(None)
    diff = "\n".join(
        difflib.unified_diff(previous.splitlines(), output.splitlines(), "previous", "current", lineterm="")
    )
    event = f"<diff>\n{diff}\n</diff>\n<output>\n{output}\n</output>"
    if len(event.encode()) > max_bytes:
        raise PrecheckFailed(f"precheck diff and output exceed the {max_bytes}-byte event limit")
    return Decision(event)


def precheck_of(trigger: Trigger) -> Precheck | None:
    """存下的前置检查（写入时已按 Precheck 校验）。"""
    return None if trigger.precheck is None else Precheck.model_validate(trigger.precheck)


def set_precheck(trigger: Trigger, precheck: Precheck | None) -> None:
    """换上（或去掉）检查：新的检查从新的基线开始比较；旧命令的失败也不记到新配置上（新命令失败时要能再告警一次）。"""
    trigger.precheck = None if precheck is None else precheck.model_dump()
    trigger.precheck_output = None
    trigger.precheck_error = None


async def current_in(s: AsyncSession, trigger_id: int, checked: dict[str, Any]) -> Trigger | None:
    """检查结果是否仍然有效：检查期间触发器被删除、停用或改了检查，这次的结果（成功或失败）都作废，返回 None。
    比较的是第一次读出的原始配置，而不是重新序列化的结果。"""
    trigger = await s.get(Trigger, trigger_id, options=[undefer(Trigger.precheck_output)])
    if trigger is None or not trigger.enabled or trigger.precheck != checked:
        log.info("trigger changed during its precheck, result discarded", extra={"trigger_id": trigger_id})
        return None
    return trigger


def passed(trigger: Trigger, precheck: Precheck, output: str, max_bytes: int, now: datetime) -> Decision:
    """检查正常结束（current_in 取到的触发器）：判定这次触发不触发（decide），记下检查时间、清掉之前的失败，changed
    检查存下这次的输出作基线。判定本身失败（差异过大）时抛 PrecheckFailed、不改状态，由调用方在另一个事务里记失败。"""
    decision = decide(precheck, output, trigger.precheck_output, max_bytes)
    if trigger.precheck_error is not None:
        log.info(
            "trigger precheck recovered",
            extra={"trigger": trigger.name, "previous_error": trigger.precheck_error},
        )
    trigger.last_checked_at, trigger.precheck_error = now, None
    if precheck.fire_when == "changed":
        trigger.precheck_output = output
    return decision


def failed(trigger: Trigger, error: str, now: datetime) -> bool:
    """记下检查失败的原因；返回是否要告警：只在从正常变为出错时告警一次（每分钟检查一次的触发器坏了不会每分钟告警）。"""
    healthy_before = trigger.precheck_error is None
    trigger.last_checked_at, trigger.precheck_error = now, error
    log.warning(
        "trigger precheck failed: %s", error, extra={"trigger": trigger.name, "alert": healthy_before}
    )
    return healthy_before


def precheck_alert(trigger: Trigger, error: str) -> str:
    """前置检查从正常变为出错时交给触发器所属 agent 的信件：出错的是哪个检查、原因，以及怎么处理。"""
    precheck = precheck_of(trigger)
    if precheck is None:
        raise RuntimeError(f"trigger {trigger.name} has no precheck to alert about")
    lines = [
        f"The precheck of your trigger `{trigger.name}` (kind {trigger.kind}) just started failing, so the "
        "trigger did not fire this time. You wrote this check, so it is yours to fix; the user has not been told.",
        "",
        f"fire_when: {precheck.fire_when}",
        f"secrets: {', '.join(precheck.secrets) or '(none)'}",
        "command:",
        "```bash",
        precheck.command,
        "```",
        "error:",
        "```",
        error,
        "```",
        "",
        "What to do:",
    ]
    user_only = (
        "- Something only the user can fix (a missing secret, a permission, an account): `notify_user` with what "
        "is needed."
    )
    if trigger.finished_at is None:
        lines += [
            "- A transient fault (network blip, a remote service briefly down): nothing to fix, the next check "
            "recovers by itself; consider adding retries to the command if this keeps happening.",
            "- A bug in the command: fix it with `trigger_update` (new precheck) and verify with `trigger_run`.",
            user_only,
            "You are told only when the check goes from healthy to failing; while it keeps failing no further "
            "message comes.",
        ]
    else:
        # 已归档的一次性触发器不能再修改或运行：任务没做，要么自己做，要么重建
        lines += [
            "- This was a one-off trigger and it has used up its single firing, so its task was NOT done; it is "
            "archived and can no longer be updated or run. If the cause was transient or a bug in the command, "
            "do the task yourself or create a new trigger with a fixed precheck.",
            user_only,
            "Its prompt was:",
            "<prompt>",
            trigger.prompt,
            "</prompt>",
        ]
    return "\n".join(lines)


class PrecheckRunner:
    def __init__(self, vault: Vault, workspace_of: Callable[[str], Path], timeout: float, max_bytes: int):
        self._vault = vault
        self._workspace_of = workspace_of
        self._timeout = timeout
        self._max_bytes = max_bytes

    async def check(self, agent_id: str, precheck: Precheck) -> None:
        """创建或修改时就确认 secret 可见：配置错误在第一现场报出，而不是等到第一次触发。"""
        await self._vault.resolve_many(agent_id, precheck.secrets)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    async def run(self, agent_id: str, precheck: Precheck, payload: str | None) -> str:
        """执行检查，返回它的输出；检查本身失败时抛出 PrecheckFailed。是否触发由 decide 决定。"""
        try:
            secrets = await self._vault.resolve_many(agent_id, precheck.secrets)
        except NotFound as e:
            raise PrecheckFailed(f"precheck secrets unavailable: {e}") from e
        started = time.monotonic()
        result = await run_bash(
            precheck.command,
            cwd=self._workspace_of(agent_id),
            secrets=secrets,
            timeout=self._timeout,
            stdin=(payload or "").encode(),
            # 「curl … | jq」里 curl 失败、jq 对空输入照样退出 0：不开 pipefail 时检查的故障会变成静默的「没事」
            pipefail=True,
        )
        ctx = {
            "agent": agent_id,
            "exit_code": result.exit_code,
            "seconds": round(time.monotonic() - started, 2),
        }
        if result.exit_code is None:
            raise PrecheckFailed(f"precheck timed out after {self._timeout}s")
        if result.exit_code != 0:
            raise PrecheckFailed(
                f"precheck exited with {result.exit_code}: {result.stderr[-_STDERR_TAIL_CHARS:].strip()}"
            )
        size = len(result.stdout.encode())
        if size > self._max_bytes:
            raise PrecheckFailed(
                f"precheck output is {size} bytes, over the {self._max_bytes}-byte event limit"
            )
        log.info("precheck done", extra={**ctx, "bytes": size})
        return result.stdout
