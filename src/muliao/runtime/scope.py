from dataclasses import dataclass
from pathlib import Path

from muliao.models import Origin, ReplyTo


@dataclass(frozen=True)
class RunScope:
    """一次 agent 运行的上下文，作为 pydantic-ai 的 deps 传给所有工具。"""

    agent_id: str
    workspace: Path
    # 反思运行不属于任何 session，以下三项均为 None
    session_id: str | None
    origin: Origin | None
    # 这一轮的回答交给谁
    reply_to: ReplyTo | None
