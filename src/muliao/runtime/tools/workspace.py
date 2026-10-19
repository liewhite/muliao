"""文件与 shell 工具。相对路径以 agent 工作目录为基准；不做强隔离（单人本机使用，见架构文档）。"""

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import FunctionToolset, RunContext

from muliao.errors import InvalidInput, NotFound
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.transcript import truncate
from muliao.vault import Vault

# shell 只继承这些环境变量，不把服务进程的其余环境带给命令
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
_LIST_LIMIT = 500
_DRAIN_SECONDS = 5.0


@dataclass(frozen=True)
class ShellLimits:
    default_timeout: float
    max_timeout: float
    max_output_chars: int


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # 进程恰好已经退出


def resolve(scope: RunScope, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else scope.workspace / p


def redact(text: str, secrets: dict[str, str]) -> str:
    for key, value in secrets.items():
        text = text.replace(value, f"[secret:{key}]")
    return text


def workspace_toolset(vault: Vault, limits: ShellLimits) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_file(ctx: RunContext[RunScope], path: str, offset: int = 0, limit: int = 2000) -> str:
        """Read a text file. Relative paths are resolved against your workspace.

        Args:
            path: File path.
            offset: First line to return (0-based).
            limit: Maximum number of lines to return.
        """
        target = resolve(ctx.deps, path)
        if not target.is_file():
            raise NotFound(f"file not found: {target}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset : offset + limit]
        body = "\n".join(f"{i + offset + 1}\t{line}" for i, line in enumerate(selected))
        more = len(lines) - offset - len(selected)
        return truncate(body, limits.max_output_chars) + (f"\n… {more} more lines" if more > 0 else "")

    @ts.tool
    async def write_file(ctx: RunContext[RunScope], path: str, content: str) -> str:
        """Create or overwrite a text file. Parent directories are created as needed.

        Args:
            path: File path.
            content: Full file content.
        """
        target = resolve(ctx.deps, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {target}"

    @ts.tool
    async def edit_file(ctx: RunContext[RunScope], path: str, old_text: str, new_text: str) -> str:
        """Replace text in a file. `old_text` must occur exactly once.

        Args:
            path: File path.
            old_text: Exact text to replace.
            new_text: Replacement text.
        """
        target = resolve(ctx.deps, path)
        if not target.is_file():
            raise NotFound(f"file not found: {target}")
        content = target.read_text(encoding="utf-8")
        count = content.count(old_text) if old_text else 0
        if count != 1:
            raise InvalidInput(f"old_text must occur exactly once in {target}, found {count}")
        target.write_text(content.replace(old_text, new_text), encoding="utf-8")
        return f"edited {target}"

    @ts.tool
    async def list_files(ctx: RunContext[RunScope], path: str = ".", recursive: bool = False) -> str:
        """List directory entries. Directories end with '/'.

        Args:
            path: Directory path.
            recursive: List all descendants instead of direct children.
        """
        target = resolve(ctx.deps, path)
        if not target.is_dir():
            raise NotFound(f"directory not found: {target}")
        entries = sorted(target.rglob("*") if recursive else target.iterdir())
        lines = [f"{e.relative_to(target)}{'/' if e.is_dir() else ''}" for e in entries[:_LIST_LIMIT]]
        if len(entries) > _LIST_LIMIT:
            lines.append(f"… {len(entries) - _LIST_LIMIT} more entries")
        return "\n".join(lines) or "(empty)"

    @ts.tool
    async def run_shell(
        ctx: RunContext[RunScope],
        command: str,
        timeout: float | None = None,  # noqa: ASYNC109  工具参数，面向模型的命名
        secrets: list[str] | None = None,
    ) -> str:
        """Run a shell command (bash) in your workspace and return its exit code and combined output.

        Args:
            command: The command line to run.
            timeout: Seconds before the command is killed.
            secrets: Vault keys to expose to the command as environment variables of the same name.
                Secret values in the output are replaced by `[secret:KEY]`.
        """
        timeout = timeout or limits.default_timeout
        if not 0 < timeout <= limits.max_timeout:
            raise InvalidInput(f"timeout must be in (0, {limits.max_timeout}] seconds")
        secret_values = await vault.resolve_many(secrets or [])
        env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ} | secret_values
        ctx.deps.workspace.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=ctx.deps.workspace,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            status = f"exit_code: {proc.returncode}"
        except TimeoutError:
            _kill_group(proc.pid)
            try:
                # 脱离进程组的后代可能仍持有管道，收尾读取也要有时限
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=_DRAIN_SECONDS)
            except TimeoutError:
                out = b""
            status = f"killed after {timeout}s timeout"
        except asyncio.CancelledError:
            _kill_group(proc.pid)
            raise
        text = redact(out.decode(errors="replace"), secret_values)
        return f"{status}\n{truncate(text, limits.max_output_chars)}"

    return ts
