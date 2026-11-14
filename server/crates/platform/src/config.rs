//! 配置：单一 TOML 文件（默认 `~/.muliao/config.toml`），是所有运行参数（含 LLM 密钥、登录密码）的唯一来源，不读环境
//! 变量。拼错或多余的键直接报错，而不是静默忽略。模板见仓库根目录 `config.example.toml`。

use std::collections::BTreeMap;
use std::fmt;
use std::num::NonZeroUsize;
use std::path::{Path, PathBuf};

use chrono_tz::Tz;
use serde::Deserialize;

pub const DEFAULT_CONFIG_PATH: &str = "~/.muliao/config.toml";

/// 密钥类的值：Debug 不打印内容，只能经 [`Secret::expose`] 取用。
#[derive(Clone, PartialEq, Eq)]
pub struct Secret(String);

impl<'de> Deserialize<'de> for Secret {
    /// 类型不对时只说「要字符串」，不带上写错的值（serde 默认把它印进报错，那可能就是密钥）
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl serde::de::Visitor<'_> for Visitor {
            type Value = Secret;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a string")
            }
            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Secret, E> {
                Ok(Secret(v.to_owned()))
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Secret, E> {
                Err(E::custom("a secret must be a string (quote it)"))
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Secret, E> {
                Err(E::custom("a secret must be a string (quote it)"))
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Secret, E> {
                Err(E::custom("a secret must be a string (quote it)"))
            }
        }
        d.deserialize_str(Visitor)
    }
}

impl Secret {
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("<secret>")
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("cannot read {path}: {source}")]
    Read { path: PathBuf, source: std::io::Error },
    #[error("invalid config {path}: {message}")]
    Invalid { path: PathBuf, message: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Default)]
#[serde(rename_all = "UPPERCASE")]
pub enum LogLevel {
    Debug,
    #[default]
    Info,
    Warning,
    Error,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    /// 对外访问地址：通知点击跳转、cookie 是否 Secure
    pub public_url: String,
}

fn default_host() -> String {
    "127.0.0.1".into()
}
fn default_port() -> u16 {
    8000
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthConfig {
    /// 至少 8 个字符
    pub password: Secret,
    /// cookie 签名密钥，至少 32 个字符
    pub secret_key: Secret,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct VaultConfig {
    /// Fernet key；丢失后 vault 中的 secret 无法解密
    pub key: Secret,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PushConfig {
    /// VAPID subject，Web Push 协议要求为 mailto: 或 https: URL
    pub vapid_subject: String,
}

/// 模型接口的协议（agenter 支持的两种）。anthropic 总是放 prompt caching 的缓存断点，不设开关（#824）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum Protocol {
    #[serde(rename = "anthropic")]
    Anthropic,
    #[serde(rename = "openai-responses")]
    OpenAiResponses,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProviderConfig {
    #[serde(rename = "type")]
    pub protocol: Protocol,
    pub api_key: Secret,
    /// 为空时用该协议的官方地址；OpenAI 兼容服务填其地址
    pub base_url: Option<String>,
    /// 同时进行的模型请求上限（这个 provider 下所有模型、所有 agent 共用；流式请求在整个流期间占一个）
    #[serde(default = "default_max_concurrency")]
    pub max_concurrency: NonZeroUsize,
}

fn default_max_concurrency() -> NonZeroUsize {
    const { NonZeroUsize::new(4).unwrap() }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LlmConfig {
    /// 新建 agent 时的默认模型，格式 "<provider 名>:<模型名>"
    pub default_model: ModelRef,
    pub providers: BTreeMap<String, ProviderConfig>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WebProviderType {
    Deepseek,
}

/// 网页搜索与抓取服务。不配置时没有 web 工具组。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WebConfig {
    #[serde(rename = "type")]
    pub kind: WebProviderType,
    pub api_key: Secret,
    /// 为空时用该类型的官方地址
    pub base_url: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TranscriptionType {
    /// 阿里云百炼的录音文件识别
    Dashscope,
}

/// 语音转文字：界面录下的语音先转成文字，再作为消息发给 agent。不配置时界面没有语音输入。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TranscriptionConfig {
    #[serde(rename = "type")]
    pub kind: TranscriptionType,
    pub api_key: Secret,
    /// 语音识别模型，如 qwen-audio-3.0-asr-flash
    pub model: String,
    /// 为空时用该类型的官方地址（dashscope：https://dashscope.aliyuncs.com/api/v1）
    pub base_url: Option<String>,
    /// 一段语音的字节上限：音频以 base64 内联在请求里，百炼限制编码后 10 MB
    #[serde(default = "default_max_audio_bytes")]
    pub max_bytes: u64,
    /// 即时热词：词 → 权重（1–5，50 为超级热词）
    #[serde(default)]
    pub vocabulary: BTreeMap<String, i64>,
    /// 语种提示（如 ["zh", "en"]）；为空时自动识别
    #[serde(default)]
    pub language_hints: Vec<String>,
}

fn default_max_audio_bytes() -> u64 {
    7 * 1024 * 1024
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct RuntimeConfig {
    // 没有重试次数的配置：会话运行与推送的暂时性失败都在约 30 分钟的窗口里退避重试（各自领域的常量）。Python 版的
    // task_max_attempts、task_retry_*、outage_retry_max_attempts 随任务队列删掉，写了按未知键报错
    /// 发给模型的上下文超过此值，就在步与步之间压缩早期上下文（下限 1000）
    pub compaction_threshold_tokens: u64,
    /// 压缩时保留最近这么多步原文不压缩
    pub compaction_keep_steps: u32,
    pub memory_max_chars: usize,
    /// 触发器前置检查的时限：检查本该便宜
    pub precheck_timeout_seconds: f64,
    pub tool_output_max_chars: usize,
    /// 单个文件（用户上传的附件、agent 发出的文件）的字节上限
    pub upload_max_bytes: u64,
}

impl Default for RuntimeConfig {
    fn default() -> Self {
        Self {
            compaction_threshold_tokens: 100_000,
            compaction_keep_steps: 10,
            memory_max_chars: 4000,
            precheck_timeout_seconds: 30.0,
            tool_output_max_chars: 20_000,
            upload_max_bytes: 50 * 1024 * 1024,
        }
    }
}

fn default_data_dir() -> PathBuf {
    "data".into()
}
fn default_assets_dir() -> PathBuf {
    "assets".into()
}
fn default_timezone() -> String {
    "UTC".into()
}
fn default_log_max_mb() -> u64 {
    50
}
fn default_log_backups() -> u32 {
    4
}

/// 配置文件原样的形状；[`Settings`] 是校验、换算之后的。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Raw {
    #[serde(default = "default_data_dir")]
    data_dir: PathBuf,
    #[serde(default = "default_assets_dir")]
    assets_dir: PathBuf,
    #[serde(default = "default_timezone")]
    timezone: String,
    #[serde(default)]
    log_level: LogLevel,
    log_file: Option<PathBuf>,
    #[serde(default = "default_log_max_mb")]
    log_max_mb: u64,
    #[serde(default = "default_log_backups")]
    log_backups: u32,
    server: ServerConfig,
    auth: AuthConfig,
    vault: VaultConfig,
    push: PushConfig,
    llm: LlmConfig,
    web: Option<WebConfig>,
    transcription: Option<TranscriptionConfig>,
    #[serde(default)]
    runtime: RuntimeConfig,
}

#[derive(Debug)]
pub struct Settings {
    /// 读入的配置文件（真实路径）
    pub file: PathBuf,
    /// 以下三个相对路径以配置文件所在目录为基准，这里已换成绝对路径
    pub data_dir: PathBuf,
    /// AI 资源（记忆、skill、MCP、secret、agent 配置、分发规则）的目录树
    pub assets_dir: PathBuf,
    /// 日志文件，按大小轮转；不设就写 stderr
    pub log_file: Option<PathBuf>,
    /// cron 表达式按此时区解释，也用于告诉 agent 当前本地时间
    pub timezone: Tz,
    pub log_level: LogLevel,
    pub log_max_mb: u64,
    pub log_backups: u32,
    pub server: ServerConfig,
    pub auth: AuthConfig,
    pub vault: VaultConfig,
    pub push: PushConfig,
    pub llm: LlmConfig,
    pub web: Option<WebConfig>,
    pub transcription: Option<TranscriptionConfig>,
    pub runtime: RuntimeConfig,
}

/// 一个模型：哪个 provider（配置里 `[llm.providers.<名>]`）的哪个模型。文字形式 "<provider 名>:<模型名>"（agent.toml、
/// 配置、工具参数里都这样写）。只经 [`ModelRef::parse`] 或 [`ModelRef::new`] 得到，两部分都不空。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelRef {
    provider: String,
    model: String,
}

impl ModelRef {
    pub fn new(provider: &str, model: &str) -> Result<ModelRef, String> {
        if provider.is_empty() || provider.contains(':') || model.is_empty() {
            return Err(format!(
                "model {provider:?} / {model:?}: give a provider (without ':') and a model, both non-empty"
            ));
        }
        Ok(ModelRef { provider: provider.to_owned(), model: model.to_owned() })
    }

    /// 文字形式 "<provider 名>:<模型名>"。
    pub fn parse(text: &str) -> Result<ModelRef, String> {
        match text.split_once(':') {
            Some((provider, model)) => Self::new(provider, model),
            None => Err(format!("model {text:?} must be '<provider>:<model>'")),
        }
    }

    pub fn provider(&self) -> &str {
        &self.provider
    }

    pub fn model(&self) -> &str {
        &self.model
    }
}

impl fmt::Display for ModelRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}:{}", self.provider, self.model)
    }
}

/// 配置里写成文字形式
impl<'de> Deserialize<'de> for ModelRef {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        ModelRef::parse(&String::deserialize(d)?).map_err(serde::de::Error::custom)
    }
}

/// `~/` 开头的换成家目录下的路径
pub fn expand_home(path: &str) -> PathBuf {
    match (path.strip_prefix("~/"), std::env::home_dir()) {
        (Some(rest), Some(home)) => home.join(rest),
        _ => PathBuf::from(path),
    }
}

impl Settings {
    /// 读配置文件（`~/` 开头的按家目录展开）。
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let path = expand_home(&path.to_string_lossy());
        let file =
            crate::fs::real(&path).map_err(|source| ConfigError::Read { path: path.clone(), source })?.into_path_buf();
        let text = std::fs::read_to_string(&file).map_err(|source| ConfigError::Read { path: file.clone(), source })?;
        Self::parse(&text, file)
    }

    /// 解析配置文本；file 是它所在的文件（相对路径以它所在目录为基准）。
    pub fn parse(text: &str, file: PathBuf) -> Result<Self, ConfigError> {
        let invalid = |message: String| ConfigError::Invalid { path: file.clone(), message };
        // 只报行号与原因，不用 toml 自带的格式（它把出错的那一行原文印出来，那一行可能是 api_key）
        let raw: Raw = toml::from_str(text).map_err(|e| {
            let line = e.span().map(|s| text[..s.start].matches('\n').count() + 1);
            invalid(match line {
                Some(line) => format!("line {line}: {}", e.message()),
                None => e.message().to_owned(),
            })
        })?;
        raw.validate().map_err(invalid)?;
        let timezone: Tz = raw.timezone.parse().map_err(|_| invalid(format!("unknown timezone {:?}", raw.timezone)))?;
        let base = file.parent().map(Path::to_path_buf).unwrap_or_default();
        Ok(Self {
            data_dir: base.join(&raw.data_dir),
            assets_dir: base.join(&raw.assets_dir),
            log_file: raw.log_file.map(|p| base.join(p)),
            file,
            timezone,
            log_level: raw.log_level,
            log_max_mb: raw.log_max_mb,
            log_backups: raw.log_backups,
            server: raw.server,
            auth: raw.auth,
            vault: raw.vault,
            push: raw.push,
            llm: raw.llm,
            web: raw.web,
            transcription: raw.transcription,
            runtime: raw.runtime,
        })
    }

    pub fn db_path(&self) -> PathBuf {
        self.data_dir.join("muliao.db")
    }

    /// 每个 agent 的工作目录（文件工具与 shell 的 cwd）
    pub fn workspaces_dir(&self) -> PathBuf {
        self.data_dir.join("workspaces")
    }

    /// 对话里出现的文件，内容不可变
    pub fn files_dir(&self) -> PathBuf {
        self.data_dir.join("files")
    }

    pub fn vapid_key_path(&self) -> PathBuf {
        self.data_dir.join("vapid_private.pem")
    }

    /// 只有 admin 能读写的平台文件（#539）：配置文件（登录密码、cookie 签名密钥、vault 主密钥、LLM key）、它旁边可能有的
    /// 副本 `.prev`（手工留的）、它旁边的备份目录 backups/（部署时的备份脚本约定的位置）、推送私钥。
    pub fn private_files(&self) -> Vec<PathBuf> {
        let name = self.file.file_name().unwrap_or_default().to_string_lossy();
        let dir = self.file.parent().map(Path::to_path_buf).unwrap_or_default();
        vec![self.file.clone(), dir.join(format!("{name}.prev")), dir.join("backups"), self.vapid_key_path()]
    }
}

fn at_least<T: PartialOrd + fmt::Display>(errors: &mut Vec<String>, name: &str, value: T, min: T) {
    if value < min {
        errors.push(format!("{name} must be at least {min} (got {value})"));
    }
}

fn positive(errors: &mut Vec<String>, name: &str, value: f64) {
    if value.is_nan() || value <= 0.0 {
        errors.push(format!("{name} must be greater than 0 (got {value})"));
    }
}

impl Raw {
    fn validate(&self) -> Result<(), String> {
        let mut e = Vec::new();
        at_least(&mut e, "log_max_mb", self.log_max_mb, 1);
        at_least(&mut e, "log_backups", self.log_backups, 1);
        at_least(&mut e, "auth.password length", self.auth.password.0.chars().count(), 8);
        at_least(&mut e, "auth.secret_key length", self.auth.secret_key.0.chars().count(), 32);
        at_least(&mut e, "vault.key length", self.vault.key.0.len(), 1);
        if !(self.push.vapid_subject.starts_with("mailto:") || self.push.vapid_subject.starts_with("https://")) {
            e.push("push.vapid_subject must start with 'mailto:' or 'https://'".into());
        }
        if self.llm.providers.is_empty() {
            e.push("llm.providers must have at least one provider".into());
        }
        for (name, p) in &self.llm.providers {
            at_least(&mut e, &format!("llm.providers.{name}.api_key length"), p.api_key.0.len(), 1);
        }
        let provider = self.llm.default_model.provider();
        if !self.llm.providers.contains_key(provider) {
            e.push(format!(
                "llm.default_model provider {provider:?} is not in {:?}",
                self.llm.providers.keys().collect::<Vec<_>>()
            ));
        }
        if let Some(w) = &self.web {
            at_least(&mut e, "web.api_key length", w.api_key.0.len(), 1);
        }
        if let Some(t) = &self.transcription {
            at_least(&mut e, "transcription.api_key length", t.api_key.0.len(), 1);
            at_least(&mut e, "transcription.model length", t.model.len(), 1);
            at_least(&mut e, "transcription.max_bytes", t.max_bytes, 1);
        }
        let r = &self.runtime;
        at_least(&mut e, "runtime.compaction_threshold_tokens", r.compaction_threshold_tokens, 1000);
        at_least(&mut e, "runtime.compaction_keep_steps", r.compaction_keep_steps, 1);
        at_least(&mut e, "runtime.memory_max_chars", r.memory_max_chars, 100);
        positive(&mut e, "runtime.precheck_timeout_seconds", r.precheck_timeout_seconds);
        at_least(&mut e, "runtime.tool_output_max_chars", r.tool_output_max_chars, 1000);
        at_least(&mut e, "runtime.upload_max_bytes", r.upload_max_bytes, 1);
        if e.is_empty() { Ok(()) } else { Err(e.join("; ")) }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL: &str = r#"
timezone = "Asia/Shanghai"
log_file = "muliao.log"
[server]
public_url = "http://127.0.0.1:1080"
[auth]
password = "12345678"
secret_key = "0123456789abcdef0123456789abcdef"
[vault]
key = "k"
[push]
vapid_subject = "mailto:a@b.c"
[llm]
default_model = "claude:opus"
[llm.providers.claude]
type = "anthropic"
api_key = "sk-xyz"
[runtime]
precheck_timeout_seconds = 60
"#;

    fn parse(text: &str) -> Result<Settings, ConfigError> {
        Settings::parse(text, PathBuf::from("/home/u/.muliao/config.toml"))
    }

    #[test]
    fn minimal_config_with_defaults_and_relative_paths() {
        let s = parse(MINIMAL).unwrap();
        assert_eq!(s.data_dir, PathBuf::from("/home/u/.muliao/data"));
        assert_eq!(s.log_file, Some(PathBuf::from("/home/u/.muliao/muliao.log")));
        assert_eq!(s.db_path(), PathBuf::from("/home/u/.muliao/data/muliao.db"));
        assert_eq!(s.timezone, chrono_tz::Asia::Shanghai);
        assert_eq!(s.server.port, 8000);
        assert_eq!(s.runtime.precheck_timeout_seconds, 60.0);
        assert_eq!(s.runtime.compaction_threshold_tokens, 100_000);
        assert_eq!(s.llm.providers["claude"].protocol, Protocol::Anthropic);
        assert!(s.private_files().contains(&PathBuf::from("/home/u/.muliao/config.toml.prev")));
        // 密钥不进 Debug 输出
        assert!(!format!("{s:?}").contains("sk-xyz"));
    }

    #[test]
    fn unknown_keys_and_bad_values_are_errors() {
        let typo = MINIMAL.replace("precheck_timeout_seconds", "precheck_timeout");
        assert!(parse(&typo).unwrap_err().to_string().contains("precheck_timeout"));
        let bad_model = MINIMAL.replace("claude:opus", "other:opus");
        assert!(parse(&bad_model).unwrap_err().to_string().contains("not in"));
        let bad_tz = MINIMAL.replace("Asia/Shanghai", "Mars/Base");
        assert!(parse(&bad_tz).unwrap_err().to_string().contains("timezone"));
        let google = MINIMAL.replace("type = \"anthropic\"", "type = \"google\"");
        assert!(parse(&google).is_err());
        let typo = MINIMAL.replace("type = \"anthropic\"", "type = \"anthropic\"\nmax_concurency = 2");
        let err = parse(&typo).unwrap_err().to_string();
        // provider 一节里的未知键也报错，并带行号
        assert!(err.contains("max_concurency") && err.contains("line "), "{err}");
        let numeric = MINIMAL.replace("\"sk-xyz\"", "987654321");
        let err = parse(&numeric).unwrap_err().to_string();
        assert!(err.contains("must be a string") && !err.contains("987654321"), "{err}");
        let zero = MINIMAL.replace("type = \"anthropic\"", "type = \"anthropic\"\nmax_concurrency = 0");
        assert!(parse(&zero).unwrap_err().to_string().contains("nonzero"), "zero concurrency");
        let short = MINIMAL.replace("\"12345678\"", "\"1234\"");
        assert!(parse(&short).unwrap_err().to_string().contains("password"));
    }
}
