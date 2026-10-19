from dataclasses import dataclass
from pathlib import Path

from muliao.models import Origin


@dataclass(frozen=True)
class RunScope:
    """一次 agent 运行的上下文，作为 pydantic-ai 的 deps 传给所有工具。"""

    agent_id: str
    workspace: Path
    # 反思运行不属于任何 session
    session_id: str | None
    origin: Origin | None
