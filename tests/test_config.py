import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from muliao.config import LlmConfig, load_settings
from muliao.errors import InvalidInput
from muliao.llm import OFFICIAL_BASE_URLS, ModelRegistry

EXAMPLE = Path(__file__).parents[1] / "config.example.toml"


def _filled_example(tmp_path: Path) -> Path:
    text = EXAMPLE.read_text()
    text = text.replace('password = ""', 'password = "password1"')
    text = text.replace('secret_key = ""', f'secret_key = "{"k" * 32}"')
    text = text.replace('key = ""', 'key = "vault-key"', 1)
    text = text.replace('api_key = ""', 'api_key = "sk-test"', 1)
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_example_config_is_valid_once_required_fields_are_filled(tmp_path):
    settings = load_settings(_filled_example(tmp_path))

    assert settings.server.port == 1080
    assert settings.data_dir == tmp_path / "data"  # 相对配置文件所在目录
    assert settings.llm.default_model == "claude:claude-sonnet-5-5"


def test_example_config_with_empty_secrets_is_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE.read_text())
    with pytest.raises(ValidationError, match="password"):
        load_settings(path)


def test_unknown_keys_are_rejected(tmp_path):
    path = _filled_example(tmp_path)
    path.write_text(path.read_text().replace("[server]", "[server]\nprot = 1"))
    with pytest.raises(ValidationError, match="prot"):
        load_settings(path)


def test_default_model_must_reference_configured_provider():
    with pytest.raises(ValidationError, match="default_model"):
        LlmConfig.model_validate(
            {"default_model": "nope:m", "providers": {"claude": {"type": "anthropic", "api_key": "k"}}}
        )


def test_registry_builds_model_type_from_provider_config():
    raw = tomllib.loads(
        """
        default_model = "claude:c"
        [providers.claude]
        type = "anthropic"
        api_key = "k1"
        [providers.deepseek]
        type = "openai"
        api_key = "k2"
        base_url = "https://api.deepseek.com"
        """
    )
    registry = ModelRegistry(LlmConfig.model_validate(raw))

    assert isinstance(registry.resolve("claude:claude-sonnet-5-5"), AnthropicModel)
    model = registry.resolve("deepseek:deepseek-chat")
    assert isinstance(model, OpenAIChatModel) and model.model_name == "deepseek-chat"
    with pytest.raises(InvalidInput, match="not configured"):
        registry.check("openai:gpt-5")
    with pytest.raises(InvalidInput, match="<provider>:<model>"):
        registry.check("claude")


def _registry(providers: dict) -> ModelRegistry:
    name = next(iter(providers))
    return ModelRegistry(LlmConfig.model_validate({"default_model": f"{name}:m", "providers": providers}))


def test_each_provider_type_builds_its_model_class():
    registry = _registry(
        {
            "a": {"type": "anthropic", "api_key": "k"},
            "o": {"type": "openai", "api_key": "k"},
            "r": {"type": "openai-responses", "api_key": "k"},
            "g": {"type": "google", "api_key": "k"},
        }
    )
    assert isinstance(registry.resolve("a:m"), AnthropicModel)
    assert isinstance(registry.resolve("o:m"), OpenAIChatModel)
    assert isinstance(registry.resolve("r:m"), OpenAIResponsesModel)
    assert isinstance(registry.resolve("g:m"), GoogleModel)


def test_sdk_base_url_env_vars_are_ignored(monkeypatch):
    for var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "GOOGLE_GEMINI_BASE_URL"):
        monkeypatch.setenv(var, "https://attacker.example")
    registry = _registry(
        {"a": {"type": "anthropic", "api_key": "k"}, "o": {"type": "openai", "api_key": "k"}}
    )

    assert str(registry.resolve("a:m").base_url).rstrip("/") == OFFICIAL_BASE_URLS["anthropic"]
    assert str(registry.resolve("o:m").base_url).rstrip("/") == OFFICIAL_BASE_URLS["openai"]


def test_default_model_with_empty_model_name_is_rejected():
    with pytest.raises(ValidationError, match="<provider>:<model>"):
        LlmConfig.model_validate(
            {"default_model": "a:", "providers": {"a": {"type": "anthropic", "api_key": "k"}}}
        )


def test_vapid_subject_scheme_is_checked(tmp_path):
    path = _filled_example(tmp_path)
    path.write_text(
        path.read_text().replace('vapid_subject = "https://muliao.example.com"', 'vapid_subject = "me"')
    )
    with pytest.raises(ValidationError, match="vapid_subject"):
        load_settings(path)


async def test_startup_rejects_agents_with_unresolvable_models(settings, models):
    from muliao.agents import AgentSpec
    from muliao.container import build_container

    c = build_container(settings, resolve_model=models)  # 使用真实的配置校验
    async with c.running():
        agent = await c.agents.create(
            AgentSpec(name="ok", description="d", model="fake:m", instructions="", reflect_every=5),
            author="user",
        )
    try:
        async with c.db.transaction() as s:
            from muliao.models import Agent

            (await s.get(Agent, agent.id)).model = "gone:m"  # 模拟配置里删掉了 provider
    finally:
        await c.db.engine.dispose()
    c2 = build_container(settings, resolve_model=models)
    with pytest.raises(RuntimeError, match=f"ok \\({agent.id}\\): model 'gone:m'"):
        async with c2.running():
            pass


def test_a_default_shell_timeout_above_the_maximum_is_rejected():
    import pytest
    from pydantic import ValidationError

    from muliao.config import RuntimeConfig

    with pytest.raises(ValidationError, match="shell_default_timeout"):
        RuntimeConfig(shell_default_timeout=900, shell_max_timeout=600)
