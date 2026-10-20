"""临时 agent：一次性做完一件事就丢弃，例如抓取网页并总结。

和委派的区别：委派给的是长期存在、有记忆和 skill 的 agent，会话会保留；临时 agent 在调用方这一轮里同步运行，
没有记忆、skill 与会话，工作目录是临时的，结束即删除，只有最终回答作为工具结果留下。

工作目录的清理：正常结束与失败时在 finally 里删除；删除本身失败、或进程在运行中被杀时，目录留到下次启动，
由 clear_temp_dirs 统一清空（data/tmp 只归临时 agent 使用，启动时没有临时 agent 在运行）。
"""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

import anyio
from pydantic_ai import FunctionToolset, ModelRetry, RunContext, ToolReturn
from pydantic_ai.exceptions import UsageLimitExceeded

from muliao.agent_runtime.runner import AgentRuntime, RunProfile
from muliao.agents.service import AgentService
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

log = logging.getLogger(__name__)

# 删除临时工作目录最多等这么久（在屏蔽作用域里，被取消也要删）；删不完的留给下次启动的 clear_temp_dirs
_DISCARD_SECONDS = 30.0


def _make_workspace(temp_root: Path) -> Path:
    temp_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="temp-agent-", dir=temp_root))


# 删除工作目录的唯一入口（测试可以只替换这一处）
_remove = shutil.rmtree


def clear_temp_dirs(temp_root: Path) -> None:
    """启动时逐个删除上次进程遗留的临时 agent 工作目录（崩溃或删除失败所致），记录清掉了哪些。

    删不掉的目录（例如脱离进程组的后台进程还在写）记 error 日志后跳过，不阻止启动：残留目录只占空间，
    不影响任何正确性，为它让服务起不来代价不成比例；需要人来处理时，日志里有路径与原因。
    """
    if not temp_root.exists():
        return
    for leftover in sorted(temp_root.iterdir()):
        try:
            _remove(leftover)
        except OSError as e:
            log.error(
                "leftover temp agent workspace not removable: %r", e, extra={"workspace": str(leftover)}
            )
        else:
            log.warning("removed leftover temp agent workspace", extra={"workspace": str(leftover)})


async def _discard(workspace: Path, agent_id: str, outcome: str) -> None:
    """删除失败不改变这次运行的结果或异常：记为错误，留给下次启动的 clear_temp_dirs。"""
    try:
        await asyncio.to_thread(_remove, workspace)
    except OSError as e:
        log.error(
            "temp agent workspace not removed, will be cleared on next start: %r",
            e,
            extra={"agent": agent_id, "workspace": str(workspace), "outcome": outcome},
        )
        return
    log.info(
        "temp agent discarded", extra={"agent": agent_id, "workspace": str(workspace), "outcome": outcome}
    )


def temp_agent_toolset(
    runtime: AgentRuntime, agents: AgentService, profile: RunProfile, temp_root: Path, request_limit: int
) -> FunctionToolset[RunScope]:
    """profile 是临时 agent 能用的工具：不得包含本工具集（临时 agent 不能再开临时 agent）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def run_temp_agent(
        ctx: RunContext[RunScope], task: str, instructions: str = "", model: str | None = None
    ) -> ToolReturn:
        """Run a throwaway helper agent for one self-contained job (e.g. fetch a web page and summarize it, analyze
        some files) and get its final answer. It runs now and you wait for it; several calls in one response run in
        parallel. Files it shares are shown to the user with your reply. It has no memory, skills or conversation: put everything it needs in `task`, and save anything you
        want to keep yourself.

        It is NOT a sandbox: it has the same shell access, secrets and MCP servers as you. Its default working
        directory is a temporary one that is deleted afterwards. Be careful handing it untrusted content.

        Args:
            task: The complete job description.
            instructions: Optional role or rules for the helper, e.g. "You are a careful summarizer.".
            model: `<provider>:<model>` to use, e.g. a cheaper one; defaults to your own model.
        """
        parent = await agents.get(ctx.deps.agent_id)
        workspace = await asyncio.to_thread(_make_workspace, temp_root)
        log.info(
            "temp agent started",
            extra={"agent": parent.id, "session_id": ctx.deps.session_id, "workspace": str(workspace)},
        )
        outcome = "failed"
        try:
            result = await runtime.run_temp(
                parent,
                task=task,
                instructions=instructions,
                model=parent.model if model is None else model,
                profile=profile,
                workspace=workspace,
                request_limit=request_limit,
            )
            outcome = "ok"
            return result.to_tool_return()
        except UsageLimitExceeded as e:
            # 有界运行在契约内的结局：交回调用方的模型去拆分任务，而不是让整轮失败后反复重试。
            # 这次已经共享的文件不交付（存在文件存储里但没有引用）：拆分后重做时会重新共享
            outcome = "request limit"
            log.warning(
                "temp agent hit its request limit",
                extra={"agent": parent.id, "session_id": ctx.deps.session_id, "limit": request_limit},
            )
            raise ModelRetry(
                f"the temporary agent did not finish within {request_limit} model requests ({e}); "
                "split the job into smaller tasks"
            ) from e
        except anyio.get_cancelled_exc_class():
            outcome = "cancelled"
            raise
        finally:
            # 被取消时也要删掉工作目录：清理不能被取消打断（删不掉的留给下次启动的 clear_temp_dirs）
            with anyio.CancelScope(shield=True), anyio.move_on_after(_DISCARD_SECONDS) as limited:
                await _discard(workspace, parent.id, outcome)
            if limited.cancelled_caught:
                log.error(
                    "temp agent workspace removal timed out, will be cleared on next start",
                    extra={"agent": parent.id, "workspace": str(workspace)},
                )

    return ts
