"""主动通知用户。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.scope import RunScope
from muliao.agent_runtime.tool_errors import TOOL_MAX_RETRIES
from muliao.notifications.service import Notifier
from muliao.notifications.tables import Notification


def notify_toolset(notifier: Notifier) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def notify_user(ctx: RunContext[RunScope], title: str, body: str) -> str:
        """Send a push notification to the user's devices. Tapping it opens this session.

        Args:
            title: Short title.
            body: Notification text, a few sentences at most.
        """
        await notifier.notify(
            ctx.deps.agent_id,
            Notification(
                title=title,
                body=body,
                session_id=ctx.deps.session_id,
            ),
        )
        return "notification queued"

    return ts
