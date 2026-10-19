"""pydantic-ai 消息的单条序列化。配置与 `ModelMessagesTypeAdapter` 保持一致。"""

from collections.abc import Iterable

import pydantic
from pydantic_ai.messages import ModelMessage

_MESSAGE: pydantic.TypeAdapter[ModelMessage] = pydantic.TypeAdapter(
    ModelMessage,
    config=pydantic.ConfigDict(defer_build=True, ser_json_bytes="base64", val_json_bytes="base64"),
)


def dump_message(message: ModelMessage) -> str:
    return _MESSAGE.dump_json(message).decode()


def load_messages(payloads: Iterable[str]) -> list[ModelMessage]:
    return [_MESSAGE.validate_json(p) for p in payloads]
