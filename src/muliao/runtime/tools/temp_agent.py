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

from pydantic_ai import FunctionToolset, ModelRetry, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded

from muliao.agents import AgentService
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES

log = logging.getLogger(__name__)


def _make_workspace(temp_root: Path) -> Path:
    temp_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="temp-agent-", dir=temp_root))


def clear_temp_dirs(temp_root: Path) -> None:
    """启动时清空上次进程遗留的临时 agent 工作目录（崩溃或删除失败所致），并记录清掉了哪些。"""
    if not temp_root.exists():
        return
    leftovers = sorted(p.name for p in temp_root.iterdir())
    if leftovers:
        log.warning("removing leftover temp agent workspaces", extra={"dirs": leftovers})
        shutil.rmtree(temp_root)


async def _discard(workspace: Path, agent_id: str, outcome: str) -> None:
    """删除失败不改变这次运行的结果或异常：记为错误，留给下次启动的 clear_temp_dirs。"""
    try:
        await asyncio.to_thread(shutil.rmtree, workspace)
    except OSError as e:
        log.error(
            "temp agent workspace not removed, will be cleared on next start: %r",
            e,
            extra={"agent": agent_id, "workspace": str(workspace)},
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
    ) -> str:
        """Run a throwaway helper agent for one self-contained job (e.g. fetch a web page and summarize it, analyze
        some files) and get its final answer. It runs now and you wait for it; several calls in one response run in
        parallel. It has no memory, skills or conversation: put everything it needs in `task`, and save anything you
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
            answer = await runtime.run_temp(
                parent,
                task=task,
                instructions=instructions,
                model=parent.model if model is None else model,
                profile=profile,
                workspace=workspace,
                request_limit=request_limit,
            )
            outcome = "ok"
            return answer
        except UsageLimitExceeded as e:
            # 有界运行在契约内的结局：交回调用方的模型去拆分任务，而不是让整轮失败后反复重试
            outcome = "request limit"
            raise ModelRetry(
                f"the temporary agent did not finish within {request_limit} model requests ({e}); "
                "split the job into smaller tasks"
            ) from e
        finally:
            await _discard(workspace, parent.id, outcome)

    return ts
