class DomainError(Exception):
    """领域层错误的基类。API 层映射为 4xx（上游失败为 502）；工具层映射为 ModelRetry 交还给模型修正。"""


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass


class InvalidInput(DomainError):
    pass


class Unauthorized(DomainError):
    """调用方无法证明自己的身份（如事件触发器不存在或 token 不匹配）。"""


class Forbidden(DomainError):
    """身份明确，但无权执行该操作（如续写不是自己发起的会话）。"""


class TooLarge(DomainError):
    """输入超过契约规定的大小上限。"""


class UpstreamFailed(DomainError):
    """同步调用的外部服务（如 LLM provider）报错或不可达；原因原样带出，由调用方决定是否重试。"""


class TransientError(Exception):
    """外部依赖按其契约可能发生的暂时性失败（5xx、限流、超时）。由任务队列统一重试。"""
