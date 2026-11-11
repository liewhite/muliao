"""agent 领域对外发布的事件（platform.messaging）。设计见 docs/architecture.md 第 10 节。"""

from muliao.platform.messaging import DomainEvent


class AgentDeleting(DomainEvent):
    """agent 正在删除：订阅它的领域各自清理自己引用它的数据；会话领域删完它的会话后发布 AgentSessionsCleared，agent
    领域再删除这一行与资源目录。"""

    TYPE = "agent.deleting"
    agent_id: str
