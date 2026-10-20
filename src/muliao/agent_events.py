"""agent 领域对外发布的事件（platform.messaging）。设计见 docs/domain-design.md 4.4。"""

from muliao.platform.messaging import DomainEvent


class AgentDeleting(DomainEvent):
    """agent 正在删除：各领域清理自己引用它的数据（会话、触发器、私有 secret、反思）；会话全部删除后 agent 领域删除
    这一行与资源目录（AgentSessionsCleared）。"""

    TYPE = "agent.deleting"
    agent_id: str
