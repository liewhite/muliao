"""agent 的思考档位（agent.toml 的 thinking），运行时交给 pydantic_ai 的 ModelSettings.thinking
（agent_runtime.llm.model_settings）："default" 什么都不传，按模型与服务端的默认；"off" 关闭；其余是 effort 档位。各
provider 怎么换算、模型不支持时怎么处理都由 pydantic_ai 与服务端决定，这里不映射。"""

from typing import Literal, get_args

Thinking = Literal["default", "off", "low", "medium", "high", "xhigh"]
THINKING_LEVELS: tuple[Thinking, ...] = get_args(Thinking)
