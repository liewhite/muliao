"""配置：单一 TOML 文件，是所有运行参数（含 LLM 密钥、登录密码）的唯一来源，不读环境变量。

模板见仓库根目录 `config.example.toml`。
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

DEFAULT_CONFIG_PATH = Path("~/.muliao/config.toml")
# 前端构建产物：`cd web && npm run build` 生成到包内，随 wheel 分发；不是用户配置
WEB_DIR = Path(__file__).parent / "webui"


def split_model(model: str) -> tuple[str, str]:
    """把 "<provider 名>:<模型名>" 拆开；两部分都不能为空。配置校验与模型构造共用。"""
    provider, sep, name = model.partition(":")
    if not sep or not provider or not name:
        raise ValueError(f"model {model!r} must be '<provider>:<model>'")
    return provider, name


class _Section(BaseModel):
    # 拼错的键直接报错，而不是静默忽略
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 8000
    # 对外访问地址：通知点击跳转、cookie 是否 Secure
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

    @model_validator(mode="after")
    def _subject_scheme(self) -> "PushConfig":
        if not self.vapid_subject.startswith(("mailto:", "https://")):
            raise ValueError("vapid_subject must start with 'mailto:' or 'https://'")
        return self


ProviderType = Literal["anthropic", "openai", "openai-responses", "google"]


class ProviderConfig(_Section):
    type: ProviderType
    api_key: SecretStr = Field(min_length=1)
    # 为空时显式使用该类型的官方地址（llm.OFFICIAL_BASE_URLS），不会回退到 SDK 的环境变量；
    # OpenAI 兼容服务（DeepSeek、OpenRouter、vLLM…）填其地址
    base_url: str | None = None
    # 只对 type = "anthropic" 有意义：是否在请求里放 prompt caching 的缓存断点（llm.ANTHROPIC_CACHE_SETTINGS）。
    # 其他协议下填它多半是误解（它不会生效），所以显式填写即报错，而不是静默忽略
    prompt_cache: bool = True

    @model_validator(mode="after")
    def _prompt_cache_only_for_anthropic(self) -> "ProviderConfig":
        if self.type != "anthropic" and "prompt_cache" in self.model_fields_set:
            raise ValueError(f'prompt_cache only applies to type = "anthropic", not {self.type!r}')
        return self


class LlmConfig(_Section):
    # 新建 agent 时的默认模型，格式 "<provider 名>:<模型名>"
    default_model: str
    providers: dict[str, ProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_model_resolvable(self) -> "LlmConfig":
        provider, _ = split_model(self.default_model)
        if provider not in self.providers:
            raise ValueError(f"default_model provider {provider!r} is not in {sorted(self.providers)}")
        return self


WebProviderType = Literal["deepseek"]


class WebConfig(_Section):
    """网页搜索与抓取服务（muliao.agent_runtime.web）。不配置时没有 web 工具组。"""

    type: WebProviderType
    api_key: SecretStr = Field(min_length=1)
    # 为空时显式使用该类型的官方地址
    base_url: str | None = None


TranscriptionType = Literal["openai"]


class TranscriptionConfig(_Section):
    """语音转文字（muliao.speech）：界面录下的语音先转成文字，再作为消息发给 agent。不配置时界面没有语音输入。

    type 是调用协议：openai = OpenAI Chat Completions 的音频输入（input_audio，流式返回），
    也适用于兼容它的服务，如阿里云百炼的 Qwen-Omni（compatible-mode）。"""

    type: TranscriptionType
    base_url: str
    api_key: SecretStr = Field(min_length=1)
    # 能听懂音频的对话模型，如 qwen3.8-omni-flash
    model: str = Field(min_length=1)
    # 一段语音的字节上限：音频以 base64 内联在请求里，服务端对请求体有上限（百炼为 10 MB 左右）
    max_bytes: int = Field(default=7 * 1024 * 1024, ge=1)


class RuntimeConfig(_Section):
    worker_concurrency: int = Field(default=4, ge=1)
    task_max_attempts: int = Field(default=5, ge=1)
    task_retry_base_seconds: float = Field(default=5.0, gt=0)
    # 单个 task 的总时限（一次运行含所有工具调用）
    task_timeout_seconds: float = Field(default=1800.0, gt=0)
    # 新建 agent 的默认反思间隔（步，即模型回复数）
    default_reflect_every: int = Field(default=30, ge=1)
    # 发给模型的上下文（上一次请求的输入 token）超过此值，就在步与步之间压缩该 session 的早期上下文
    compaction_threshold_tokens: int = Field(default=100_000, ge=1000)
    # 压缩时保留最近这么多步原文不压缩（一步是一次模型回复及其工具结果）
    compaction_keep_steps: int = Field(default=10, ge=1)
    memory_max_chars: int = Field(default=4000, ge=100)
    shell_default_timeout: float = Field(default=120.0, gt=0)
    shell_max_timeout: float = Field(default=600.0, gt=0)
    # 触发器前置检查的时限：webhook 的调用方在同步等它，检查本该便宜，比 shell 的默认时限短得多
    precheck_timeout_seconds: float = Field(default=30.0, gt=0)
    # 后台任务（jobs.runner）运行期间，每隔这么久给发起它的会话发一个检查事件，由 agent 决定继续等还是结束它
    job_check_seconds: float = Field(default=60.0, gt=0)
    tool_output_max_chars: int = Field(default=20000, ge=1000)
    # 临时 agent 单次运行最多的模型请求数：做不完就尽早失败，交回调用方拆分任务，而不是靠整轮超时反复重试
    temp_agent_request_limit: int = Field(default=30, ge=1)
    # 单个文件（用户上传的附件、agent 发出的文件）的字节上限，默认与 Matrix Synapse 一致
    upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> "RuntimeConfig":
        # 默认时限超过上限时，每次不带 timeout 的 run_shell 都会被拒绝
        if self.shell_default_timeout > self.shell_max_timeout:
            raise ValueError(
                f"shell_default_timeout ({self.shell_default_timeout}) exceeds shell_max_timeout ({self.shell_max_timeout})"
            )
        return self


class Settings(_Section):
    # 相对路径以配置文件所在目录为基准（见 load_settings）
    data_dir: Path = Path("data")
    # cron 表达式按此时区解释，也用于告诉 agent 当前本地时间
    timezone: str = "UTC"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    server: ServerConfig
    auth: AuthConfig
    vault: VaultConfig
    push: PushConfig
    llm: LlmConfig
    web: WebConfig | None = None
    transcription: TranscriptionConfig | None = None
    runtime: RuntimeConfig = RuntimeConfig()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "muliao.db"

    @property
    def resources_dir(self) -> Path:
        """agent 能改的文本资源（指令、记忆、skill、MCP 配置），是一个 git 仓库，见 muliao.agents.resources。"""
        return self.data_dir / "resources"

    @property
    def workspaces_dir(self) -> Path:
        """每个 agent 的工作目录（文件工具与 shell 的 cwd），不做版本管理。"""
        return self.data_dir / "workspaces"

    @property
    def temp_dir(self) -> Path:
        """临时 agent 的工作目录在这里创建，用完即删。"""
        return self.data_dir / "tmp"

    @property
    def files_dir(self) -> Path:
        """对话里出现的文件，内容不可变，见 muliao.files.store。"""
        return self.data_dir / "files"

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
