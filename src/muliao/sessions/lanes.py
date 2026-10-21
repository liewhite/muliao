"""会话领域自己命名的 lane（docs/architecture.md「任务队列即 actor 调度」）：lane 名在任务表里是全局的，会话领域命名的
都在这里定义，别的领域不拼它们（要问会话忙不忙，用 SessionActivity）。没有给 lane 的订阅与命令（如 StopThread）用平台
默认的按接收方一条 lane。"""


def session_lane(session_id: str) -> str:
    """会话 actor 的邮箱：运行、压缩都在这条 lane 上串行执行。"""
    return f"session:{session_id}"


def receive_lane(session_id: str) -> str:
    """投给一个会话的 Receive 命令：同一会话的来信按发送顺序投递，不同会话之间互不阻塞。不是会话自己的 lane：投信
    不排在会话正在进行的运行后面。"""
    return f"sessions.receive:{session_id}"


def keyed_receive_lane(origin: str, origin_ref: str, agent_id: str) -> str:
    """按发送方定位会话的 Receive（inbound.SessionKey）：会话可能还不存在，lane 按 key 命名。同一个 key 的来信串行投递，
    「没有就新建」不会建出两个。"""
    return f"sessions.receive:{origin}:{origin_ref}:{agent_id}"


def work_lane(holder: str) -> str:
    """会话之外的一项工作的结束与运行中检查（WorkEnded、WorkCheckIn）：检查不会排在结束之后。"""
    return f"sessions.work:{holder}"


def agent_deleting_lane(agent_id: str) -> str:
    """删除 agent 流程里会话领域的一步（AgentSessionsCleanup）。"""
    return f"sessions.agent_deleting:{agent_id}"
