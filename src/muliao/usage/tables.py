"""用量领域的表：每次模型运行一行，只增不改。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Index, String
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref, session_ref


class RunKind(StrEnum):
    """哪一类运行用掉的 token（agent_runtime 的四个模型调用入口各对应一个）。"""

    # 会话的一次运行（AgentRuntime.run_steps）
    RUN = "run"
    # 上下文压缩的一次总结（summarize）
    COMPACTION = "compaction"
    # 强制反思（run_once）
    REFLECTION = "reflection"
    # 临时 agent（run_temp）
    TEMP_AGENT = "temp_agent"


class ModelUsage(Base):
    """一次模型运行报告的用量。

    行永久保留，也不随 agent 或会话的删除而消失：它是账目，删掉资源不等于这笔钱没花过（所以 agent_id 与
    session_id 和别的领域一样只是不透明的 id，可能指向已经不存在的东西）。

    token 的口径沿用 pydantic-ai 的归一化约定（pydantic_ai.usage）：input_tokens 是输入总数，**已经包含**
    cache_read_tokens 与 cache_write_tokens，展示时三者是总数与其中两部分的关系，不要相加。
    """

    __tablename__ = "model_usage"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    agent_id: Mapped[str] = mapped_column(agent_ref())
    # 不属于任何会话的运行（反思）为空；临时 agent 记发起它的那个会话
    session_id: Mapped[str | None] = mapped_column(session_ref())
    kind: Mapped[RunKind] = mapped_column(StrEnumType(RunKind))
    # "<provider 名>:<模型名>"，与 agents.model 同一格式
    model: Mapped[str] = mapped_column(String(200))
    requests: Mapped[int]
    input_tokens: Mapped[int]
    output_tokens: Mapped[int]
    cache_read_tokens: Mapped[int]
    cache_write_tokens: Mapped[int]

    # 汇总一律按时间范围取行，再在范围内分组
    __table_args__ = (Index("ix_model_usage_created_at", "created_at"), {"sqlite_autoincrement": True})
