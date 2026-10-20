import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def new_agent_id() -> str:
    """agent 的 id：12 位小写十六进制，短到模型能可靠复制，用作主键、目录名与工具参数。"""
    return uuid.uuid4().hex[:12]
