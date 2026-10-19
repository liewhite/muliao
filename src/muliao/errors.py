class DomainError(Exception):
    """领域层错误的基类。API 层映射为 4xx；工具层映射为 ModelRetry 交还给模型修正。"""


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class InvalidInput(DomainError):
    pass


class TransientError(Exception):
    """外部依赖按其契约可能发生的暂时性失败（5xx、限流、超时）。由任务队列统一重试。"""
