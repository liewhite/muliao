"""配置：单一 TOML 文件，是所有运行参数（含 LLM 密钥、登录密码）的唯一来源，不读环境变量。

模板见仓库根目录 `config.example.toml`。
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

DEFAULT_CONFIG_PATH = Path("~/.muliao/config.toml")


class _Section(BaseModel):
    # 拼错的键直接报错，而不是静默忽略
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 8000
    # 对外访问地址：通知点击跳转、webhook URL、cookie 是否 Secure
    public_url: str


class AuthConfig(_Section):
    password: SecretStr = Field(min_length=8)
    # cookie 签名密钥
    secret_key: SecretStr = Field(min_length=32)


class VaultConfig(_Section):
    # Fernet key；丢失后 vault 中的 secret 无法解密
    key: SecretStr = Field(min_length=1)


class PushConfig(_Section):
    # VAPID subject，Web Push 协议要求为 mailto: 或 https: URL
    vapid_subject: str


ProviderType = Literal["anthropic", "openai", "openai-responses", "google"]


class ProviderConfig(_Section):
    type: ProviderType
    api_key: SecretStr = Field(min_length=1)
    # 为空时使用该提供方的官方地址；OpenAI 兼容服务（DeepSeek、OpenRouter、vLLM…）填其地址
    base_url: str | None = None


class LlmConfig(_Section):
    # 新建 agent 时的默认模型，格式 "<provider 名>:<模型名>"
    default_model: str
    providers: dict[str, ProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_model_resolvable(self) -> "LlmConfig":
        provider, sep, _ = self.default_model.partition(":")
        if not sep or provider not in self.providers:
            raise ValueError(
                f"default_model {self.default_model!r} must be '<provider>:<model>' "
                f"with provider in {sorted(self.providers)}"
            )
        return self


class RuntimeConfig(_Section):
    worker_concurrency: int = 4
    task_max_attempts: int = 5
    task_retry_base_seconds: float = 5.0
    # 单个 task 的总时限（一轮 turn 含所有工具调用）
    task_timeout_seconds: float = 1800.0
    default_reflect_every: int = 10
    memory_max_chars: int = 4000
    shell_default_timeout: float = 120.0
    shell_max_timeout: float = 600.0
    tool_output_max_chars: int = 20000


class TelegramConfig(_Section):
    bot_token: SecretStr = Field(min_length=1)
    # 接收消息的 agent
    agent: str
    allowed_chat_ids: list[int] = Field(min_length=1)


class Settings(_Section):
    # 相对路径以配置文件所在目录为基准（见 load_settings）
    data_dir: Path = Path("data")
    # cron 表达式按此时区解释，也用于告诉 agent 当前本地时间
    timezone: str = "UTC"
    log_level: str = "INFO"
    # 前端构建产物：`cd web && npm run build` 生成到包内，随 wheel 分发
    web_dir: Path = Path(__file__).parent / "webui"

    server: ServerConfig
    auth: AuthConfig
    vault: VaultConfig
    push: PushConfig
    llm: LlmConfig
    runtime: RuntimeConfig = RuntimeConfig()
    telegram: TelegramConfig | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "muliao.db"

    @property
    def public_skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def agents_dir(self) -> Path:
        return self.data_dir / "agents"

    @property
    def vapid_key_path(self) -> Path:
        return self.data_dir / "vapid_private.pem"


def load_settings(path: Path) -> Settings:
    path = path.expanduser().resolve()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    settings = Settings.model_validate(raw)
    if not settings.data_dir.is_absolute():
        settings = settings.model_copy(update={"data_dir": path.parent / settings.data_dir})
    return settings
