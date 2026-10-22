import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from muliao.agent_runtime.llm import KINDS, ModelRegistry
from muliao.config import LlmConfig, load_settings
from muliao.errors import InvalidInput

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

    assert isinstance(registry.resolve("claude:claude-sonnet-5-5").wrapped, AnthropicModel)
    model = registry.resolve("deepseek:deepseek-chat")
    assert isinstance(model.wrapped, OpenAIChatModel) and model.model_name == "deepseek-chat"
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
    assert isinstance(registry.resolve("a:m").wrapped, AnthropicModel)
    assert isinstance(registry.resolve("o:m").wrapped, OpenAIChatModel)
    assert isinstance(registry.resolve("r:m").wrapped, OpenAIResponsesModel)
    assert isinstance(registry.resolve("g:m").wrapped, GoogleModel)


def test_models_of_one_provider_share_its_concurrency_limit():
    """模型请求的并发上限按 provider：同一 provider 的所有模型（所有 agent）共用一个，provider 之间互不影响；
    默认 4，可在 provider 上配置。"""
    registry = _registry(
        {
            "a": {"type": "anthropic", "api_key": "k"},
            "o": {"type": "openai", "api_key": "k", "max_concurrency": 2},
        }
    )
    a1, a2, o = registry.resolve("a:m1"), registry.resolve("a:m2"), registry.resolve("o:m")
    assert all(isinstance(m, ConcurrencyLimitedModel) for m in (a1, a2, o))
    assert a1._limiter is a2._limiter and a1._limiter is not o._limiter  # noqa: SLF001
    assert registry._limiters["a"]._limiter.max_running == 4  # noqa: SLF001
    assert registry._limiters["o"]._limiter.max_running == 2  # noqa: SLF001


async def test_a_model_request_over_the_limit_waits_and_the_wait_is_logged(caplog):
    """超过上限的请求排队，而不是失败；排队与等了多久记日志，慢是排队还是模型本身从日志能分清。"""
    import anyio

    from muliao.agent_runtime.llm import _ProviderLimiter

    caplog.set_level("INFO", logger="muliao.agent_runtime.llm")
    limiter = _ProviderLimiter("a", 1)
    await limiter.acquire("model:m")
    got = anyio.Event()

    async def second() -> None:
        await limiter.acquire("model:m")
        got.set()

    async with anyio.create_task_group() as group:
        group.start_soon(second)
        await anyio.sleep(0.05)
        assert not got.is_set()
        limiter.release()
        with anyio.fail_after(5):
            await got.wait()
        limiter.release()
    messages = [r.getMessage() for r in caplog.records if r.name == "muliao.agent_runtime.llm"]
    assert messages == ["model request waiting for a slot", "model request got a slot"]


def test_sdk_base_url_env_vars_are_ignored(monkeypatch):
    for var in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "GOOGLE_GEMINI_BASE_URL"):
        monkeypatch.setenv(var, "https://attacker.example")
    registry = _registry(
        {"a": {"type": "anthropic", "api_key": "k"}, "o": {"type": "openai", "api_key": "k"}}
    )

    assert str(registry.resolve("a:m").base_url).rstrip("/") == KINDS["anthropic"].official_base_url
    assert str(registry.resolve("o:m").base_url).rstrip("/") == KINDS["openai"].official_base_url


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
    from muliao.agents.service import AgentSpec
    from muliao.container import assemble

    c = assemble(settings, resolve_model=models)  # 使用真实的配置校验
    async with c.running():
        agent = await c.agents.create(
            AgentSpec(name="ok", description="d", model="fake:m", instructions=""),
            author="user",
        )
    try:
        async with c.db.transaction() as s:
            from muliao.agents.tables import Agent

            (await s.get(Agent, agent.id)).model = "gone:m"  # 模拟配置里删掉了 provider
    finally:
        await c.db.engine.dispose()
    c2 = assemble(settings, resolve_model=models)
    with pytest.raises(RuntimeError, match=f"ok \\({agent.id}\\): model 'gone:m'"):
        async with c2.running():
            pass


def test_a_default_shell_timeout_above_the_maximum_is_rejected():
    import pytest
    from pydantic import ValidationError

    from muliao.config import RuntimeConfig

    with pytest.raises(ValidationError, match="shell_default_timeout"):
        RuntimeConfig(shell_default_timeout=900, shell_max_timeout=600)
