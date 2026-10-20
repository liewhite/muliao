"""有会话的运行的工作区工具：文件与 shell（agent_runtime.workspace_tools）加上后台任务——run_shell 可以 background，
job_status、job_kill。后台任务属于会话（结局投回发起它的会话），所以只组合进有会话的运行。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.api import SHELL_DESCRIPTION, ShellLimits, file_toolset, run_foreground
from muliao.errors import InvalidInput
from muliao.files.api import FileStore
from muliao.jobs.runner import JobRunner
from muliao.jobs.tables import JobStatus
from muliao.platform.tooling import RunScope
from muliao.vault.api import Vault

_BACKGROUND_DESCRIPTION = (
    " For anything longer, or to keep working while it runs, pass `background: true`: it returns a job id at once "
    "and has no time limit; its output goes to a log file in your workspace, you get an event when it finishes and "
    "a check-in event about every {check:g} seconds while it runs (decide then whether to keep waiting or call "
    "`job_kill`)."
)


def session_workspace_toolset(
    vault: Vault, limits: ShellLimits, files: FileStore, jobs: JobRunner
) -> FunctionToolset[RunScope]:
    """文件、shell 与后台任务。"""
    ts = file_toolset(limits, files)

    @ts.tool(
        description=SHELL_DESCRIPTION.format(default=limits.default_timeout, max=limits.max_timeout)
        + _BACKGROUND_DESCRIPTION.format(check=jobs.check_seconds)
    )
    async def run_shell(
        ctx: RunContext[RunScope],
        command: str,
        timeout: float | None = None,  # noqa: ASYNC109  工具参数，面向模型的命名
        secrets: tuple[str, ...] = (),
        background: bool = False,
    ) -> str:
        """The description is set in the decorator (it carries the configured time limits).

        Args:
            command: The command line to run.
            timeout: Seconds before the command is killed (foreground only).
            secrets: Vault keys (your private ones or public ones) to expose to the command as environment
                variables of the same name.
                Secret values in the output are replaced by `[secret:KEY]`.
            background: Run it as a background job instead of waiting for it.
        """
        if not background:
            return await run_foreground(vault, limits, ctx.deps, command, timeout, secrets)
        if timeout is not None:
            raise InvalidInput(
                "background jobs have no timeout; leave `timeout` out (kill them with job_kill)"
            )
        if ctx.deps.session_id is None:
            # 这组工具只组合进有会话的运行
            raise RuntimeError(f"session workspace tools used outside a session by agent {ctx.deps.agent_id}")
        job = await jobs.start(
            session_id=ctx.deps.session_id,
            agent_id=ctx.deps.agent_id,
            command=command,
            secrets=list(secrets),
            awaited=ctx.deps.user_waits,
        )
        return (
            f"started background job {job.id}; output goes to {job.log_path}. "
            "You will get an event when it finishes; keep working or end your reply."
        )

    @ts.tool
    async def job_status(ctx: RunContext[RunScope], job_id: int) -> str:
        """Show a background job's status and the last lines of its output (read its log file for all of it).

        Args:
            job_id: The id `run_shell(background: true)` returned.
        """
        info = await jobs.info(job_id, ctx.deps.agent_id)
        state = f"exited with code {info.exit_code}" if info.status == JobStatus.EXITED else info.status.value
        return (
            f"job {info.id}: {state} ({info.running_for})\ncommand: {info.command}\nlog: {info.log_path}\n"
            f"last lines:\n{info.tail or '(no output yet)'}"
        )

    @ts.tool
    async def job_kill(ctx: RunContext[RunScope], job_id: int) -> str:
        """Kill a running background job (its whole process tree). Its final status arrives as an event.

        Args:
            job_id: The id `run_shell(background: true)` returned.
        """
        await jobs.kill(job_id, ctx.deps.agent_id)
        return f"kill signal sent to job {job_id}; its final status will arrive as an event"

    return ts
