"""以 agent 身份跑命令（#618）：run_shell 前台与后台任务、触发器的前置检查与 stream 都经这里起 agent 的命令。

一条命令以谁的身份跑由三件事一起决定，只在这里拼：注入哪些 secret（Vault.resolve_many，shell 决定能不能注入
allow_shell = false 的）、在哪个目录（agent 的工作目录）、关在什么写沙箱里（Confiner.of）。命令失败时被沙箱拒绝的写
补什么说明也只在这里定（AgentShell.refusal，按同一份沙箱）：跑到结束的命令随结果给出（CommandResult.refusal），起了不等
的由调用方在它结束时拿退出码与输出来问；调用方只负责把说明拼在自己的报错后面。怎么起进程、读输出、杀进程组是
platform.shell 的事，这里不管。

MCP 的 stdio server 不走这里：它由 fastmcp 起（不是 bash），secret 按使用者解析、沙箱按使用者或 owner 取，组合根直接
把 Confiner.of 注入给它（agent_runtime.mcp）。
"""

import asyncio
import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from muliao.agents.api import Vault
from muliao.platform.sandbox import Confinement
from muliao.platform.shell import ShellResult, run_bash, spawn_bash
from muliao.workspace.writable import Confiner, denial_note


@dataclass(frozen=True, kw_only=True)
class CommandResult(ShellResult):
    """AgentShell.run 的结果：在 ShellResult 之外带上被写沙箱拦下的写的说明（AgentShell.refusal），没有则 None。"""

    refusal: str | None


@dataclass(frozen=True)
class AgentShell:
    """一个 agent 起命令的环境（AgentCommands.prepare 给出）：工作目录、注入的 secret 明文（调用方脱敏自己记下的输出
    时用）、写沙箱。起命令前现算，改了授权下一条命令就生效。"""

    cwd: Path
    secrets: Mapping[str, str]
    _confinement: Confinement

    async def spawn(
        self, command: str, *, stdin: bool = False, merge_stderr: bool = False, pipefail: bool = False
    ) -> asyncio.subprocess.Process:
        """起命令、不等它结束（后台任务、stream）：进程组与输出由调用方处置（platform.shell.spawn_bash）。"""
        return await spawn_bash(
            command,
            cwd=self.cwd,
            secrets=self.secrets,
            confinement=self._confinement,
            stdin=stdin,
            merge_stderr=merge_stderr,
            pipefail=pipefail,
        )

    async def run(
        self,
        command: str,
        *,
        timeout: float,  # noqa: ASYNC109  调用方给出的时限，超时杀掉进程组（platform.shell.run_bash）
        stdin: bytes | None = None,
        merge_stderr: bool = False,
        pipefail: bool = False,
    ) -> CommandResult:
        """跑到结束（前台 run_shell、前置检查），输出里的 secret 已替换，失败时带上沙箱拒绝的说明（看报错的那路输出：
        merge_stderr 时是 stdout，否则 stderr）。"""
        result = await run_bash(
            command,
            cwd=self.cwd,
            secrets=self.secrets,
            timeout=timeout,
            confinement=self._confinement,
            stdin=stdin,
            merge_stderr=merge_stderr,
            pipefail=pipefail,
        )
        errors = result.stdout if merge_stderr else result.stderr
        fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
        return CommandResult(**fields, refusal=self.refusal(result.exit_code, errors))

    def refusal(self, exit_code: int | None, output: str) -> str | None:
        """命令结束后补在报错后面的说明：没成功（退出码非 0，或超时、被杀没有退出码）且输出（报错的那部分）里有被写沙箱
        拦下的写时给出，否则 None（workspace.writable.denial_note）。退出码 0 不看：命令成功了，输出里引用的
        「Operation not permitted」不是它失败的原因。spawn 起的命令（后台任务、stream）由调用方在它结束时来问。"""
        return None if exit_code == 0 else denial_note(self._confinement, output)


class AgentCommands:
    def __init__(self, vault: Vault, workspace_of: Callable[[str], Path], confiner: Confiner):
        self._vault = vault
        self._workspace_of = workspace_of
        self._confiner = confiner

    async def prepare(self, agent_id: str, secret_keys: Sequence[str], *, shell: bool) -> AgentShell:
        """agent 起命令的环境。shell：命令是 agent 自己写的（run_shell、触发器），不是 skill_run 拼出的脚本调用。
        secret 不存在或不能交给这条命令时抛 NotFound / InvalidInput（Vault.resolve_many），什么都还没起。"""
        secrets = await self._vault.resolve_many(agent_id, list(secret_keys), shell=shell)
        return AgentShell(self._workspace_of(agent_id), secrets, await self._confiner.of(agent_id))
