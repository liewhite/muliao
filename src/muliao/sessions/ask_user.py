"""向用户提问：一次几个选择题，用户在界面上回答（见 sessions.questions）。"""

from pydantic_ai import CallDeferred, FunctionToolset, RunContext
from pydantic_ai.tools import ToolDefinition

from muliao.agent_runtime.scope import RunScope
from muliao.agent_runtime.tool_errors import TOOL_MAX_RETRIES
from muliao.sessions.questions import ASK_USER, Questions


async def _only_when_the_user_reads(ctx: RunContext[RunScope], tool: ToolDefinition) -> ToolDefinition | None:
    """只有这一轮的回答交给用户时才有人能回答：ask_agent 的私有 thread、触发器、反思与临时 agent 里不提供。"""
    return tool if ctx.deps.user_waits else None


def ask_user_toolset() -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool(name=ASK_USER, prepare=_only_when_the_user_reads)
    async def ask_user(
        ctx: RunContext[RunScope],
        questions: Questions,
    ) -> str:
        """Ask the user one to four multiple-choice questions when you need their decision or preference to
        continue, e.g. which option to pursue or which constraint matters. They can always answer "other" in their
        own words, so do not add an "Other" option. Put all questions in one call. Your turn ends once this response's tool calls finish; the
        answers arrive as this tool's result when the user replies. Don't ask what you can reasonably decide or
        look up yourself.

        Args:
            questions: The questions, each with 2-6 options; set multi_select when several may apply.
        """
        raise CallDeferred()

    return ts
