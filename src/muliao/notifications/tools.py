"""主动通知用户。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.notifications.inbox import Notifications
from muliao.platform.tool_views import MarkdownBlock, ToolRender, rendered_by
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

_NOTIFY_RENDER = ToolRender[str](call=lambda a: [MarkdownBlock(title=a["title"], text=a["body"])])


def notify_toolset(notifications: Notifications) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool(metadata=rendered_by(_NOTIFY_RENDER))
    async def notify_user(ctx: RunContext[RunScope], title: str, body: str) -> str:
        """Send a push notification to the user's devices. Tapping it opens this session.

        Args:
            title: Short title.
            body: Notification text, a few sentences at most.
        """
        await notifications.notify(ctx.deps.agent_id, title, body, ctx.deps.session_id)
        return "queued notification to the user"

    return ts
