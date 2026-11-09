"""工作区工具：文件工具（file_tools）加上 run_shell（前台，或 background 作为后台任务）、job_status、job_kill。
后台任务属于发起它的会话（结局投回那个会话）。

前台命令是阻塞的（调用不返回、一直等）：必须给 timeout；后台任务与 agent 并发：没有 timeout，按 heartbeat_seconds
投运行中检查，由 agent 决定继续等还是 job_kill。"""

import asyncio
import os
import shlex
from pathlib import PurePosixPath

from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.api import is_approved, require_approval
from muliao.agents.api import AssetSource, SkillScripts, SkillStore, Vault
from muliao.errors import InvalidInput, NotFound
from muliao.files.api import FileStore
from muliao.platform.shell import LEFT_RUNNING_NOTE, run_bash
from muliao.platform.text_edit import truncate
from muliao.platform.tool_views import TerminalBlock, ToolRender, ViewBlock, rendered_by
from muliao.platform.tooling import RunScope
from muliao.sessions.api import DEFAULT_CHECK_SECONDS
from muliao.workspace.file_tools import ShellLimits, file_toolset
from muliao.workspace.runner import JobRunner
from muliao.workspace.tables import JobStatus
from muliao.workspace.writable import Confiner, denial_note

_SHELL_DESCRIPTION = (
    "Run a shell command (bash) in your workspace and return its exit code and combined output. "
    "By default it runs in the foreground, which is blocking: you wait until it ends, so you must pass `timeout`, "
    "the seconds it may take (estimate it yourself, there is no upper limit: e.g. 30 for a quick query, 600 for a "
    "test suite). If it is still running then, it is killed and you get `killed after Ns timeout` with the output "
    "so far. It ends when bash exits: processes it leaves running with `&` are not killed, but their output after "
    "that is not shown (redirect it to a file if you need it). "
    "Only the first {max_output} characters of output are kept, so limit it yourself: pipe through `| head` or `| tail`, "
    "use `rg -m N`, `cut -c1-200`, and read only the part of a long log you need. "
    "Search with ripgrep, not grep/find: `rg -n PATTERN [PATH]` for contents (`-g '*.py'` filters files, `-C 3` "
    "adds context), `rg --files [-g GLOB]` to find files; it skips .git and ignored files such as node_modules. "
    "`pgrep -f` / `pkill -f` also match the shell running your command, since its command line contains the "
    "pattern (here and over ssh on a remote host), so `pkill -f` can kill that shell: bracket one character, e.g. "
    "`pgrep -f '[a]rb live'`, or match the process name exactly with `pgrep -x`."
)

_BACKGROUND_DESCRIPTION = (
    " To keep working while a long command runs, pass `background: true` and no `timeout`: this is non-blocking. "
    "It returns a job id at once and the job runs alongside you with no time limit; its output goes to "
    "jobs/<id>.log in your workspace. While it runs you get a check-in event every `heartbeat_seconds` (default "
    "60; choose it from how long you expect the job to take, e.g. 300 for a half-hour build) with its run time and "
    "last output lines: then either keep waiting (if nothing needs doing, reply with just '…') or stop it with "
    "`job_kill`; `job_status` shows it at any time. When it ends you get an event with its exit code and last "
    "output."
)


_SKILL_RUN_DESCRIPTION = (
    "Run a script from one of your skills: `<skill directory>/<script>` with `args`, executed directly (no shell "
    "parsing; it needs a shebang line and the executable bit). It runs like run_shell (in your workspace, only "
    "able to write where your commands may write, same output limit, same `timeout` / `background` / "
    "`heartbeat_seconds` rules). `secrets` works as in run_shell and also takes secrets marked "
    "`allow_shell = false`, which run_shell, trigger prechecks and streams refuse: such a key is meant to be used "
    "only through a script that wraps the operation with its own checks, so use the skill's script for it "
    "instead of writing the command yourself. A script listed under `approval:` in the skill's SKILL.md "
    "frontmatter waits for the user to approve or deny this exact call before it runs: your turn ends once this "
    "response's tool calls finish, and the script's result (or the denial) arrives as this tool's result."
)


def _script_command(
    scripts: SkillScripts, skill: str, script: str, args: tuple[str, ...]
) -> tuple[str, bool]:
    """skill 目录里的脚本加参数，拼成一条不经 shell 解析的命令，以及它执行前要不要用户批准。脚本必须落在目录里
    （经符号链接也不行）。"""
    directory = scripts.directory
    root = directory.resolve()
    path = (root / script).resolve()
    if PurePosixPath(script).is_absolute() or not path.is_relative_to(root) or path == root:
        raise InvalidInput(
            f"script must be a path inside the skill's directory ({directory}), got {script!r}"
        )
    if not path.is_file():
        raise NotFound(f"skill {skill!r} has no script {script!r} (looked for {path})")
    if not os.access(path, os.X_OK):
        raise InvalidInput(
            f"{path} is not executable: a skill script needs a shebang line and the executable bit (chmod +x)"
        )
    return shlex.join([str(path), *args]), scripts.needs_approval(path)


def workspace_toolset(
    vault: Vault,
    limits: ShellLimits,
    files: FileStore,
    jobs: JobRunner,
    confiner: Confiner,
    assets: AssetSource,
    skills: SkillStore,
) -> FunctionToolset[RunScope]:
    """文件、shell、skill 脚本与后台任务。"""
    ts = file_toolset(limits, files, confiner, assets)
    # 输出上限是配置的值，写进说明：模型不知道它，长输出被截了才学会
    description = _SHELL_DESCRIPTION.format(max_output=limits.max_output_chars) + _BACKGROUND_DESCRIPTION

    @ts.tool(description=description, metadata=rendered_by(SHELL_RENDER))
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
                default 60).
        """
        return await execute(ctx.deps, command, timeout, secrets, background, heartbeat_seconds, shell=True)

    @ts.tool(description=_SKILL_RUN_DESCRIPTION, metadata=rendered_by(SKILL_RUN_RENDER))
    async def skill_run(
        ctx: RunContext[RunScope],
        skill: str,
        script: str,
        args: tuple[str, ...] = (),
        timeout: float | None = None,  # noqa: ASYNC109  工具参数，与 run_shell 同名同义
        secrets: tuple[str, ...] = (),
        background: bool = False,
        heartbeat_seconds: float | None = None,
    ) -> str:
        """The description is set in the decorator.

        Args:
            skill: Name of a skill in your skill index.
            script: Path of the script relative to the skill's directory, e.g. `scripts/deploy.py`.
            args: Arguments passed to the script as they are (no shell parsing).
            timeout: As in run_shell: foreground only, and required there (seconds, no upper limit).
            secrets: As in run_shell (vault keys you can use, exposed as environment variables of the same name,
                values in the output replaced by `[secret:KEY]`), and also keys marked `allow_shell = false`.
            background: As in run_shell: run it as a background job instead of waiting for it.
            heartbeat_seconds: As in run_shell: background only, seconds between check-in events (default 60).
        """
        found = await skills.scripts(skill, ctx.deps.agent_id)
        command, gated = await asyncio.to_thread(_script_command, found, skill, script, args)
        return await execute(
            ctx.deps,
            command,
            timeout,
            secrets,
            background,
            heartbeat_seconds,
            shell=False,
            ask_approval=gated and not is_approved(ctx),
        )

    async def execute(
        scope: RunScope,
        command: str,
        timeout: float | None,  # noqa: ASYNC109  工具参数原样传入
        secrets: tuple[str, ...],
        background: bool,
        heartbeat_seconds: float | None,
        *,
        shell: bool,
        ask_approval: bool = False,
    ) -> str:
        """run_shell 与 skill_run 共用的执行：前台或后台、参数用错场合的报错都一样。shell：command 是 agent 自己写的
        （决定能不能注入 allow_shell = false 的 secret，Vault.resolve_many）。ask_approval：参数校验通过后先停下等用户批准
        （require_approval），不去问一个参数就不对的调用。"""
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
            if ask_approval:
                require_approval()
            return await _run_foreground(vault, limits, confiner, scope, command, timeout, secrets, shell)
        if timeout is not None:
            raise InvalidInput(
                "background jobs have no timeout; leave `timeout` out (set `heartbeat_seconds` for how often "
                "you want to hear about it, kill it with job_kill)"
            )
        check = DEFAULT_CHECK_SECONDS if heartbeat_seconds is None else heartbeat_seconds
        if not check > 0:  # 也拒绝 NaN
            raise InvalidInput("heartbeat_seconds must be greater than 0")
        if ask_approval:
            require_approval()
        job = await jobs.start(
            session_id=scope.session_id,
            agent_id=scope.agent_id,
            command=command,
            secrets=list(secrets),
            shell=shell,
            awaited=scope.user_waits,
            check_seconds=check,
        )
        return (
            f"{_STARTED} {job.id}; output goes to {job.log_path}.\n"
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
        return f"sent kill signal to job {job_id}; its final status will arrive as an event"

    return ts


async def _run_foreground(
    vault: Vault,
    limits: ShellLimits,
    confiner: Confiner,
    scope: RunScope,
    command: str,
    timeout: float,  # noqa: ASYNC109  run_shell 的参数原样传入（前台命令必填）
    secrets: tuple[str, ...],
    shell: bool,
) -> str:
    if not timeout > 0:  # 也拒绝 NaN
        raise InvalidInput("timeout must be greater than 0 seconds")
    secret_values = await vault.resolve_many(scope.agent_id, list(secrets), shell=shell)
    confinement = await confiner.of(scope.agent_id)
    result = await run_bash(
        command,
        cwd=scope.workspace,
        secrets=secret_values,
        timeout=timeout,
        confinement=confinement,
        merge_stderr=True,
    )
    status = (
        f"{_EXIT_CODE} {result.exit_code}"
        if result.exit_code is not None
        else f"{_KILLED} {timeout}s timeout"
    )
    output = truncate(result.stdout, limits.max_output_chars)
    if (note := denial_note(confinement, result.stdout)) is not None:
        # 紧接在系统报错之后：输出以换行结尾就不再空一行
        output += note if output.endswith("\n") else f"\n{note}"
    if result.left_running:
        output += f"\n[{LEFT_RUNNING_NOTE}]"
    return f"{status}\n{output}"


# run_shell 结果首行的开头：上面的工具写出、下面的展示规则认出，只在这里定义。首行总是工具自己写的一行状态，
# 命令的输出从第二行起
_EXIT_CODE = "exit_code:"
_KILLED = "killed after"
_STARTED = "started background job"


def _shell_result(text: str) -> list[ViewBlock] | None:
    """前台：首行是退出码或超时被杀，其余是输出；后台：首行是启动了哪个任务。认不出就显示原文。"""
    head, _, output = text.partition("\n")
    if head.startswith(_EXIT_CODE):
        code = head.removeprefix(_EXIT_CODE).strip()
        return [TerminalBlock(command=None, output=output, status=f"exit {code}", failed=code != "0")]
    if head.startswith(_KILLED):
        return [TerminalBlock(command=None, output=output, status=head, failed=True)]
    if head.startswith(_STARTED):
        return [TerminalBlock(command=None, output=None, status=head, failed=False)]
    return None


SHELL_RENDER = ToolRender[str](
    call=lambda a: [TerminalBlock(command=a["command"], output=None, status=None, failed=False)],
    result=_shell_result,
)

SKILL_RUN_RENDER = ToolRender[str](
    call=lambda a: [
        TerminalBlock(
            command=shlex.join([f"{a['skill']}/{a['script']}", *a["args"]]),
            output=None,
            status=None,
            failed=False,
        )
    ],
    result=_shell_result,
)
