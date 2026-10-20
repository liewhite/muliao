"""pydantic-ai 消息的单条序列化。配置与 `ModelMessagesTypeAdapter` 保持一致。"""

from collections.abc import Iterable

import pydantic
from pydantic_ai.messages import ModelMessage

_MESSAGE: pydantic.TypeAdapter[ModelMessage] = pydantic.TypeAdapter(
    ModelMessage,
    config=pydantic.ConfigDict(defer_build=True, ser_json_bytes="base64", val_json_bytes="base64"),
)


# 序列化后消息种类所在的字段与模型回复的取值（pydantic-ai 的 ModelResponse.kind）：库里按它数步数（reflection.service），
# tests/test_stores.py 固定这个格式，升级 pydantic-ai 时格式变了会在测试里暴露，不会让计数悄悄归零
KIND_PATH = "$.kind"
RESPONSE_KIND = "response"


def dump_message(message: ModelMessage) -> str:
    return _MESSAGE.dump_json(message).decode()


def load_messages(payloads: Iterable[str]) -> list[ModelMessage]:
    return [_MESSAGE.validate_json(p) for p in payloads]
