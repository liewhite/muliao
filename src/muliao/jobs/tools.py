"""工作区工具：文件与前台 shell（agent_runtime.workspace_tools）加上后台任务——run_shell 可以 background，
job_status、job_kill。后台任务属于发起它的会话（结局投回那个会话）。

前台命令是阻塞的（调用不返回、一直等）：必须给 timeout；后台任务与 agent 并发：没有 timeout，按 heartbeat_seconds
投运行中检查，由 agent 决定继续等还是 job_kill。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.api import ShellLimits, file_toolset, run_foreground, shell_description
from muliao.errors import InvalidInput
from muliao.files.api import FileStore
from muliao.jobs.runner import JobRunner
from muliao.jobs.tables import JobStatus
from muliao.platform.checks import DEFAULT_CHECK_SECONDS
from muliao.platform.tooling import RunScope
from muliao.vault.api import Vault

_BACKGROUND_DESCRIPTION = (
    " To keep working while a long command runs, pass `background: true` and no `timeout`: this is non-blocking. "
    "It returns a job id at once and the job runs alongside you with no time limit; its output goes to "
    "jobs/<id>.log in your workspace. While it runs you get a check-in event every `heartbeat_seconds` (default "
    "30; choose it from how long you expect the job to take, e.g. 300 for a half-hour build) with its run time and "
    "last output lines: then either keep waiting (if nothing needs doing, reply with just '…') or stop it with "
    "`job_kill`; `job_status` shows it at any time. When it ends you get an event with its exit code and last "
    "output."
)


def workspace_toolset(
    vault: Vault, limits: ShellLimits, files: FileStore, jobs: JobRunner
) -> FunctionToolset[RunScope]:
    """文件、shell 与后台任务。"""
    ts = file_toolset(limits, files)

    @ts.tool(description=shell_description(limits) + _BACKGROUND_DESCRIPTION)
    async def run_shell(
        ctx: RunContext[RunScope],
        command: str,
        timeout: float | None = None,  # noqa: ASYNC109  工具参数，面向模型的命名
        secrets: tuple[str, ...] = (),
        background: bool = False,
        heartbeat_seconds: float | None = None,
    ) -> str:
        """The description is set in the decorator (it carries the configured limits).

        Args:
            command: The command line to run.
            timeout: Foreground only, and required there: seconds before the command is killed (> 0, no upper
                limit; estimate it yourself). Leave it out with `background: true`.
            secrets: Vault keys you can use (your own, or allowed by your roles or your own allow rules) to
                expose to the command as environment variables of the same name.
                Secret values in the output are replaced by `[secret:KEY]`.
            background: Run it as a background job instead of waiting for it.
            heartbeat_seconds: Background only: seconds between check-in events while the job runs (> 0,
                default 30).
        """
        if not background:
            if heartbeat_seconds is not None:
                raise InvalidInput(
                    "heartbeat_seconds is only for background jobs; a foreground command uses `timeout`"
                )
            if timeout is None:
                raise InvalidInput(
                    "timeout is required for a foreground command: estimate how many seconds it needs "
                    "(no upper limit), or run it with `background: true`"
                )
            return await run_foreground(vault, limits, ctx.deps, command, timeout, secrets)
        if timeout is not None:
            raise InvalidInput(
                "background jobs have no timeout; leave `timeout` out (set `heartbeat_seconds` for how often "
                "you want to hear about it, kill it with job_kill)"
            )
        check = DEFAULT_CHECK_SECONDS if heartbeat_seconds is None else heartbeat_seconds
        if not check > 0:  # 也拒绝 NaN
            raise InvalidInput("heartbeat_seconds must be greater than 0")
        job = await jobs.start(
            session_id=ctx.deps.session_id,
            agent_id=ctx.deps.agent_id,
            command=command,
            secrets=list(secrets),
            awaited=ctx.deps.user_waits,
            check_seconds=check,
        )
        return (
            f"started background job {job.id}; output goes to {job.log_path}. "
            f"You will get a check-in event every {check:g} seconds while it runs and an event when it finishes; "
            "keep working or end your reply."
        )

    @ts.tool
    async def job_status(ctx: RunContext[RunScope], job_id: int) -> str:
        """Show a background job's status (running, exited with its code, killed, interrupted), how long it has
        run and the last lines of its output (read its log file for all of it). Use it to look at a job between
        check-ins; it does not change anything.

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
        """Kill a running background job (its whole process tree), e.g. when a check-in shows it is stuck or no
        longer needed. Its final status (killed) arrives as an event.

        Args:
            job_id: The id `run_shell(background: true)` returned.
        """
        await jobs.kill(job_id, ctx.deps.agent_id)
        return f"kill signal sent to job {job_id}; its final status will arrive as an event"

    return ts
