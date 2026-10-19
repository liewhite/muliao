from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程级配置，全部来自环境变量（前缀 MULIAO_）或 .env。"""

    model_config = SettingsConfigDict(env_prefix="MULIAO_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    # 前端构建产物目录（web/ 下 `npm run build` 生成）
    web_dir: Path = Path("web/dist")
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    # 对外访问地址，用于通知点击跳转和 VAPID subject
    public_url: str = "http://localhost:8000"

    # 单用户登录密码与 cookie 签名密钥
    password: SecretStr
    secret_key: SecretStr
    # Fernet key（`Fernet.generate_key()` 生成），加密 vault
    vault_key: SecretStr
    # VAPID subject，Web Push 协议要求为 mailto: 或 https: URL
    vapid_subject: str = "mailto:admin@localhost"

    # cron 表达式按此时区解释，也用于告诉 agent 当前本地时间
    timezone: str = "UTC"

    default_model: str = "anthropic:claude-sonnet-5-5"

    worker_concurrency: int = 4
    task_max_attempts: int = 5
    task_retry_base_seconds: float = 5.0

    default_reflect_every: int = 10
    memory_max_chars: int = 4000

    shell_default_timeout: float = 120.0
    shell_max_timeout: float = 600.0
    tool_output_max_chars: int = 20000

    # Telegram 触发源；token 为空则不启用
    telegram_bot_token: SecretStr | None = None
    telegram_agent: str | None = None
    telegram_allowed_chat_ids: list[int] = []

    @model_validator(mode="after")
    def _telegram_complete(self) -> "Settings":
        if self.telegram_bot_token is not None and not self.telegram_agent:
            raise ValueError("MULIAO_TELEGRAM_AGENT is required when MULIAO_TELEGRAM_BOT_TOKEN is set")
        return self

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
