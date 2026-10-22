"""向用户提问：一次几个选择题，用户在界面上回答（见 sessions.questions）。

工具总是列出（工具列表在缓存前缀最前面，随运行增减会让整个前缀失效）；没有用户等着这次回答时
（私有 thread、触发器、运行中检查、临时 agent），调用被拒绝并交还模型。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.api import defer_call
from muliao.errors import Conflict
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.questions import ASK_USER, Questions

NO_USER_WAITING = (
    "No user is waiting for this run's answer, so nobody can answer questions here. Decide yourself, "
    "or send the user a notification (`notify_user`, if you have it) if they must know something, and carry on."
)


def ask_user_toolset() -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool(name=ASK_USER)
    async def ask_user(
        ctx: RunContext[RunScope],
        questions: Questions,
    ) -> str:
        """Ask the user one to four multiple-choice questions when you need their decision or preference to
        continue, e.g. which option to pursue or which constraint matters. They can always answer "other" in their
        own words, so do not add an "Other" option. Put all questions in one call. Your turn ends once this response's tool calls finish; the
        answers arrive as this tool's result when the user replies. Don't ask what you can reasonably decide or
        look up yourself. Only works when the run context says the user is notified with your final answer.

        Args:
            questions: The questions, each with 2-6 options; set multi_select when several may apply.
        """
        if not ctx.deps.user_waits:
            raise Conflict(NO_USER_WAITING)
        defer_call()

    return ts
