"""agent 领域对外发布的事件（platform.messaging）。设计见 docs/architecture.md 第 10 节。"""

from muliao.platform.messaging import DomainEvent


class AgentDeleting(DomainEvent):
    """agent 正在删除，在标记它的事务里发布：订阅它的领域在这个事务里清理自己引用它的数据（会话领域删除它的会话，
    有忙的就整个回滚）；agent 领域之后再删除资源目录与这一行。"""

    agent_id: str
