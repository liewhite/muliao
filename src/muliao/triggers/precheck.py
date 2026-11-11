"""触发器的前置检查：触发前先跑一条便宜、确定性的命令，只有真有事才调用 agent（所有触发方式共用）。

约定（命令在该 agent 的工作目录里用 bash -o pipefail 执行，环境与 run_shell 相同，可按 key 注入 secret；stdin 是原始事件内容：
webhook 的请求体，定时与手动触发为空）。退出码 0 时按 fire_when 决定（decide）：
- output（默认）：stdout 非空就触发，stdout 作为事件内容交给 agent（替换原始事件，可以借此过滤、裁剪）；为空不触发。
  命令可以把游标（如上次读到的消息 id）存在工作目录里，自己做增量。
- changed：与上一次的输出逐字比较，变了才触发，交给 agent 的是差异加完整输出；命令只需打印当前状态，上一次的输出
  存在触发器上。第一次运行只记录基线、不触发（没有可比较的，叫 agent 只会白花一次模型调用）。
非 0 退出、超时、输出过大、secret 不可见：检查本身失败（PrecheckFailed），不触发，原因记成触发器的出错（triggers.faults：
从正常变为出错时告警一次，检查成功时清除）。不当作「没事」跳过——那会把脚本的故障变成静默的漏触发。

检查在触发器上的状态（precheck、precheck_output 基线、last_checked_at）只在这里迁移：换检查（set_precheck）从新基线
开始；检查期间触发器变了，结果作废（current_in）；成功（passed：判定是否触发，清掉检查的出错）、失败（failed：记下检查
时间）。投信、记录出错与告警由 TriggerService 编排。
"""

import difflib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from muliao.errors import InvalidInput, NotFound, ScriptFailed
from muliao.triggers.faults import FaultSource, recovered
from muliao.triggers.tables import Trigger
from muliao.workspace.api import AgentCommands

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
    """换上（或去掉）检查：新的检查从新的基线开始比较（旧命令的出错由 TriggerService.update 按人工干预清掉）。"""
    trigger.precheck = None if precheck is None else precheck.model_dump()
    trigger.precheck_output = None


async def current_in(s: AsyncSession, trigger_id: int, checked: dict[str, Any]) -> Trigger | None:
    """检查结果是否仍然有效：检查期间触发器被删除、停用或改了检查，这次的结果（成功或失败）都作废，返回 None。
    比较的是第一次读出的原始配置，而不是重新序列化的结果。"""
    trigger = await s.get(Trigger, trigger_id, options=[undefer(Trigger.precheck_output)])
    if trigger is None or not trigger.enabled or trigger.precheck != checked:
        log.info("trigger changed during its precheck, result discarded", extra={"trigger_id": trigger_id})
        return None
    return trigger


def passed(trigger: Trigger, precheck: Precheck, output: str, now: datetime) -> None:
    """检查正常结束、判定也成功（decide 没报错）且这次要认账（current_in 取到的触发器；触发了却整次投不进的不认账）：
    记下检查时间、清掉检查的出错，changed 检查存下这次的输出作基线。"""
    trigger.last_checked_at = now
    recovered(trigger, FaultSource.PRECHECK)
    if precheck.fire_when == "changed":
        trigger.precheck_output = output


def failed(trigger: Trigger, now: datetime) -> None:
    """检查失败：记下检查时间（原因由调用方记成出错，triggers.faults）。"""
    trigger.last_checked_at = now


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
        # 已结束的一次性触发器不能再修改或运行：任务没做，要么自己做，要么重建
        lines += [
            "- This was a one-off trigger and it has used up its single firing, so its task was NOT done; it has "
            "finished and can no longer be updated or run. If the cause was transient or a bug in the command, "
            "do the task yourself or create a new trigger with a fixed precheck.",
            user_only,
            "Its prompt was:",
            "<prompt>",
            trigger.prompt,
            "</prompt>",
        ]
    return "\n".join(lines)


class PrecheckRunner:
    def __init__(self, commands: AgentCommands, timeout: float, max_bytes: int):
        self._commands = commands
        self._timeout = timeout
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    async def run(self, agent_id: str, precheck: Precheck, payload: str | None) -> str:
        """执行检查，返回它的输出；检查本身失败时抛出 PrecheckFailed。是否触发由 decide 决定。"""
        try:
            agent_shell = await self._commands.prepare(agent_id, precheck.secrets, shell=True)
        except (NotFound, InvalidInput) as e:
            raise PrecheckFailed(f"precheck secrets unavailable: {e}") from e
        started = time.monotonic()
        result = await agent_shell.run(
            precheck.command,
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
            stderr = result.stderr[-_STDERR_TAIL_CHARS:].strip()
            raise PrecheckFailed(
                f"precheck exited with {result.exit_code}: {stderr}"
                + ("" if result.refusal is None else f"\n{result.refusal}")
            )
        size = len(result.stdout.encode())
        if size > self._max_bytes:
            raise PrecheckFailed(
                f"precheck output is {size} bytes, over the {self._max_bytes}-byte event limit"
            )
        # 每分钟一次的 precheck 绝大多数是空跑，记 INFO 会占日志三成（#413）；触发与失败另有 INFO/WARNING
        log.debug("precheck done", extra={**ctx, "bytes": size})
        return result.stdout
