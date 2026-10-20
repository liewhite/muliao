"""触发器的前置检查：触发前先跑一条便宜、确定性的命令，只有真有事才调用 agent（所有触发方式共用）。

约定（命令在该 agent 的工作目录里用 bash -o pipefail 执行，环境与 run_shell 相同，可按 key 注入 secret；stdin 是原始事件内容：
webhook 的请求体，定时与手动触发为空）。退出码 0 时按 fire_when 决定（decide）：
- output（默认）：stdout 非空就触发，stdout 作为事件内容交给 agent（替换原始事件，可以借此过滤、裁剪）；为空不触发。
  命令可以把游标（如上次读到的消息 id）存在工作目录里，自己做增量。
- changed：与上一次的输出逐字比较，变了才触发，交给 agent 的是差异加完整输出；命令只需打印当前状态，上一次的输出
  存在触发器上。第一次运行只记录基线、不触发（没有可比较的，叫 agent 只会白花一次模型调用）。
非 0 退出、超时、输出过大、secret 不可见：检查本身失败（PrecheckFailed），不触发，原因记在触发器上，并在从正常变为
出错时通知用户一次。不当作「没事」跳过——那会把脚本的故障变成静默的漏触发。
"""

import difflib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from muliao.errors import DomainError, NotFound
from muliao.platform.shell import run_bash
from muliao.vault.service import Vault

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


class PrecheckFailed(DomainError):
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
