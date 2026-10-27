"""触发器领域的表。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref


class Trigger(Base):
    """触发器：具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件。

    怎么被触发由 kind 决定（定时、webhook……），只在代码里多态实现（triggers/），表里不为某种方式开专门的列：
    config 是这种方式的配置（用户或 agent 设置），state 是这种方式自己维护的运行状态（如下次触发时间、token 摘要）。
    """

    __tablename__ = "triggers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 出现在 webhook 地址里：小写字母、数字与 -。只在未完成的触发器之间唯一（见 finished_at）
    name: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(agent_ref(), index=True)
    # 交给 agent 的说明：这类事件是什么、该怎么处理；事件内容（若有）附在其后
    prompt: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_session_id: Mapped[str | None] = mapped_column(String(36))
    # 触发前的前置检查（triggers.precheck.Precheck），所有触发方式共用；None 表示每次都交给 agent
    precheck: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 最近一次跑前置检查的时间（不论结果），让用户看到「在检查、只是没事」
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # fire_when = changed 时最近一次成功检查的输出，用来比较；修改前置检查时清空（重新记录基线）。
    # 最大可到事件上限，默认不随行加载（列表、定时循环都不需要它），用到时显式 undefer
    precheck_output: Mapped[str | None] = mapped_column(Text, deferred=True)
    # 被自动停用的原因（如定时触发器的数据不合法、触发被拒绝）；重新启用时清除
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    # 最近一次前置检查失败的原因；检查成功、或换了前置检查时清除
    precheck_error: Mapped[str | None] = mapped_column(Text)
    # 结束的时间：这个触发器不会再触发了（一次性触发器用掉了它那一次）。已结束的不出现在默认列表，不能再修改、
    # 启用或立即运行；行保留着，它发起过的会话（origin_ref 是它的 id）因此仍查得到是谁发的
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        # 名称只在未结束的触发器之间唯一：结束之后同名可以再建，历史里的同名触发器各自保留
        Index("uq_triggers_name_unfinished", "name", unique=True, sqlite_where=finished_at.is_(None)),
        # id 是会话的发送方身份（sessions.origin_ref），必须永不复用
        {"sqlite_autoincrement": True},
    )
