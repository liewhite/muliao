class DomainError(Exception):
    """领域层错误的基类。API 层映射为 4xx（ScriptFailed 为 502，见 api.app._STATUS）；工具层映射为 ModelRetry 交还给模型修正，连续失败用完预算后改为 ToolFailed（agent_runtime.tool_errors）；工具拒绝调用都抛它，不直接抛 ModelRetry。"""


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


class ScriptFailed(DomainError):
    """系统替调用方执行它写的脚本（如触发器的前置检查）失败：脚本非 0 退出、超时、输出过大，或执行所需的
    secret 不可见。API 映射为 502 并带出原因（可能含脚本的 stderr，只给已登录的用户）；工具层照常交还模型，由它修改脚本。"""


class UpstreamFailed(Exception):
    """界面同步查询的外部服务（如向 LLM provider 查模型列表）报错或不可达，API 映射为 502 并原样带出原因。

    不是 DomainError：调用方纠正不了，工具层也不会把它当作「模型输入有误」交还模型。任务里调用外部依赖的暂时失败用
    TransientError，由任务队列统一重试。
    """


class CorruptState(Exception):
    """系统自己存下的数据缺失或不合法（资源文件缺失或格式损坏、引用的文件不存在）：数据与代码不一致，不是调用方的
    输入错误。不是 DomainError：不映射成 4xx，不交还模型修正；按程序错误报出，信息里带上路径或 id，等人修复。"""


class TransientError(Exception):
    """外部依赖按其契约可能发生的暂时性失败（5xx、限流、超时）。由任务队列统一重试。"""


class ModelUnavailable(TransientError):
    """模型提供方（或到它的网络）暂时不可用：模型接口的暂时性状态码、连接失败。这类故障常持续几分钟到半小时（如
    relay 503 No available accounts），任务队列按更长的重试上限重试（runtime.model_retry_max_attempts），其余
    暂时性失败（工具、推送）照常按 task_max_attempts。"""
