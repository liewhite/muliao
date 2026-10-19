import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel

from muliao.config import LlmConfig, load_settings
from muliao.errors import InvalidInput
from muliao.llm import ModelRegistry

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
