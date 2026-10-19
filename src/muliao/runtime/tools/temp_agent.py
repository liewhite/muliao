"""临时 agent：一次性做完一件事就丢弃，例如抓取网页并总结。

和委派的区别：委派给的是长期存在、有记忆和 skill 的 agent，会话会保留；临时 agent 在调用方这一轮里同步运行，
没有记忆、skill 与会话，工作目录是临时的，结束即删除，只有最终回答作为工具结果留下。
"""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from pydantic_ai import FunctionToolset, RunContext

from muliao.agents import AgentService
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES

log = logging.getLogger(__name__)


def _make_workspace(temp_root: Path) -> Path:
    temp_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="temp-agent-", dir=temp_root))


def temp_agent_toolset(
    runtime: AgentRuntime, agents: AgentService, profile: RunProfile, temp_root: Path
) -> FunctionToolset[RunScope]:
    """profile 是临时 agent 能用的工具：不得包含本工具集（临时 agent 不能再开临时 agent）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def run_temp_agent(
        ctx: RunContext[RunScope], task: str, instructions: str = "", model: str | None = None
    ) -> str:
        """Run a throwaway helper agent for one self-contained job (e.g. fetch a web page and summarize it, analyze
        a file, try something risky in a scratch directory) and get its final answer. It runs now and you wait for
        it; several calls in one response run in parallel. It has file and shell tools in a temporary directory
        that is deleted afterwards, your MCP servers and your secrets, but no memory, skills or conversation:
        put everything it needs in `task`, and save anything you want to keep yourself.

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
        try:
            return await runtime.run_temp(
                parent,
                task=task,
                instructions=instructions,
                model=parent.model if model is None else model,
                profile=profile,
                workspace=workspace,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace)
            log.info("temp agent discarded", extra={"agent": parent.id, "workspace": str(workspace)})

    return ts
