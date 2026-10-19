import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model

from muliao.config import Settings
from muliao.container import Container, build_container
from muliao.db import init_schema
from tests.scripted import scripted

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


class ModelSlot:
    """测试中按 agent 的 model 字符串返回预设的模型对象；未预设的返回固定回复 "ok" 的模型。"""

    def __init__(self) -> None:
        self.models: dict[str, Model] = {}
        self._default = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))

    def __call__(self, name: str) -> Model:
        return self.models.get(name, self._default)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "server": {"public_url": "http://test"},
            "auth": {"password": "password1", "secret_key": "k" * 32},
            "vault": {"key": Fernet.generate_key().decode()},
            "push": {"vapid_subject": "mailto:test@example.com"},
            "llm": {
                "default_model": "fake:default",
                "providers": {"fake": {"type": "anthropic", "api_key": "unused"}},
            },
            "runtime": {"task_retry_base_seconds": 0.01},
        }
    )


@pytest.fixture
def models() -> ModelSlot:
    return ModelSlot()


@pytest.fixture
async def container(settings: Settings, models: ModelSlot) -> AsyncIterator[Container]:
    c = build_container(settings, resolve_model=models, model_checker=lambda _m: None)
    await init_schema(c.db.engine)
    yield c
    await c.http.aclose()
    await c.db.engine.dispose()


@pytest.fixture
def make_agent(container: Container) -> Callable:
    from muliao.agents import AgentSpec

    async def _make(
        name: str = "alpha", *, model: str = "test", reflect_every: int = 10, description: str = "test agent"
    ):
        return await container.agents.create(
            name,
            AgentSpec(
                description=description, model=model, instructions="Be helpful.", reflect_every=reflect_every
            ),
        )

    return _make
