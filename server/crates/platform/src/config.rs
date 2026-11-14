//! 配置：单一 TOML 文件（默认 `~/.muliao/config.toml`），是所有运行参数（含 LLM 密钥、登录密码）的唯一来源，不读环境
//! 变量。拼错或多余的键直接报错，而不是静默忽略。模板见仓库根目录 `config.example.toml`。

use std::fmt;
use std::path::{Path, PathBuf};

use chrono_tz::Tz;
use serde::Deserialize;
use serde::de::DeserializeOwned;

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
pub struct VaultConfig {
    /// Fernet key；丢失后 vault 中的 secret 无法解密
    pub key: Secret,
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
    vault: VaultConfig,
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
    pub vault: VaultConfig,
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

/// 配置文件里一个领域的段（`[web]`、`[llm]`……）：类型放在用它的领域 crate 里，组合根逐段取出（[`ConfigFile::section`]），
/// 平台不知道有哪些段。
pub trait ConfigSection: DeserializeOwned {
    /// 段名（文件里的 `[<NAME>]`）
    const NAME: &'static str;
    /// 解析之后的检查（长度、取值范围、段内各项对得上）；不过时是每条问题的说明，键名写全（如 `web.api_key length`）。
    fn check(&self) -> Vec<String>;
}

/// 读入、还没拆开的配置文件。组合根先用 [`ConfigFile::section`] 取走各领域的段，再用 [`ConfigFile::finish`] 把剩下的
/// 解析成平台自己的 [`Settings`]：谁也没取走的键在那一步按未知键报错，拼错的段名不会被静默忽略。报错只给行号与原因，
/// 不带原文（出错的那一行可能是 api_key）。
pub struct ConfigFile {
    file: PathBuf,
    text: String,
    /// 已被取走的段
    taken: Vec<&'static str>,
}

impl ConfigFile {
    /// 读配置文件（`~/` 开头的按家目录展开）。
    pub fn load(path: &Path) -> Result<Self, ConfigError> {
        let path = expand_home(&path.to_string_lossy());
        let file =
            crate::fs::real(&path).map_err(|source| ConfigError::Read { path: path.clone(), source })?.into_path_buf();
        let text = std::fs::read_to_string(&file).map_err(|source| ConfigError::Read { path: file.clone(), source })?;
        Self::parse(text, file)
    }

    /// 配置文本；file 是它所在的文件（相对路径以它所在目录为基准）。语法错误在这里报。
    pub fn parse(text: impl Into<String>, file: PathBuf) -> Result<Self, ConfigError> {
        let this = Self { file, text: text.into(), taken: Vec::new() };
        this.document()?;
        Ok(this)
    }

    /// 取出一个领域的段并检查；文件里没有这一段时为 None。
    pub fn section<T: ConfigSection>(&mut self) -> Result<Option<T>, ConfigError> {
        self.taken.push(T::NAME);
        let mut document = self.document()?;
        let Some(value) = document.get_mut().remove(T::NAME) else { return Ok(None) };
        let section = T::deserialize(toml::de::ValueDeserializer::from(value)).map_err(|e| self.syntax(e))?;
        let problems = section.check();
        if !problems.is_empty() {
            return Err(self.invalid(problems.join("; ")));
        }
        Ok(Some(section))
    }

    /// 必须有的段。
    pub fn required<T: ConfigSection>(&mut self) -> Result<T, ConfigError> {
        self.section()?.ok_or_else(|| self.invalid(format!("missing section [{}]", T::NAME)))
    }

    /// 剩下的部分是平台自己的配置：没被取走的段在这里按未知键报错。
    pub fn finish(self) -> Result<Settings, ConfigError> {
        let mut document = self.document()?;
        for name in &self.taken {
            document.get_mut().remove(*name);
        }
        let raw = Raw::deserialize(toml::de::Deserializer::from(document)).map_err(|e| self.syntax(e))?;
        raw.validate().map_err(|m| self.invalid(m))?;
        let timezone: Tz =
            raw.timezone.parse().map_err(|_| self.invalid(format!("unknown timezone {:?}", raw.timezone)))?;
        let base = self.file.parent().map(Path::to_path_buf).unwrap_or_default();
        Ok(Settings {
            data_dir: base.join(&raw.data_dir),
            assets_dir: base.join(&raw.assets_dir),
            log_file: raw.log_file.map(|p| base.join(p)),
            file: self.file,
            timezone,
            log_level: raw.log_level,
            log_max_mb: raw.log_max_mb,
            log_backups: raw.log_backups,
            vault: raw.vault,
            runtime: raw.runtime,
        })
    }

    /// 整个文件（带位置：取出的段报错时的位置仍是在整个文件里的）。
    fn document(&self) -> Result<toml::Spanned<toml::de::DeTable<'_>>, ConfigError> {
        toml::de::DeTable::parse(&self.text).map_err(|e| self.syntax(e))
    }

    fn syntax(&self, e: toml::de::Error) -> ConfigError {
        let line = e.span().map(|s| self.text[..s.start].matches('\n').count() + 1);
        self.invalid(match line {
            Some(line) => format!("line {line}: {}", e.message()),
            None => e.message().to_owned(),
        })
    }

    fn invalid(&self, message: String) -> ConfigError {
        ConfigError::Invalid { path: self.file.clone(), message }
    }
}

impl Settings {
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

/// 检查下限，不够时记一条问题（各段的 [`ConfigSection::check`] 共用）。
pub fn at_least<T: PartialOrd + fmt::Display>(errors: &mut Vec<String>, name: &str, value: T, min: T) {
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
        at_least(&mut e, "vault.key length", self.vault.key.0.len(), 1);
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
[vault]
key = "k"
[demo]
api_key = "sk-xyz"
count = 2
[runtime]
precheck_timeout_seconds = 60
"#;

    /// 一个领域的段（真的段在各自的 crate 里）
    #[derive(Debug, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct Demo {
        api_key: Secret,
        count: u32,
    }

    impl ConfigSection for Demo {
        const NAME: &'static str = "demo";
        fn check(&self) -> Vec<String> {
            let mut e = Vec::new();
            at_least(&mut e, "demo.count", self.count, 1);
            e
        }
    }

    fn parse(text: &str) -> Result<(Demo, Settings), ConfigError> {
        let mut file = ConfigFile::parse(text, PathBuf::from("/home/u/.muliao/config.toml"))?;
        let demo = file.required::<Demo>()?;
        Ok((demo, file.finish()?))
    }

    #[test]
    fn minimal_config_with_defaults_and_relative_paths() {
        let (demo, s) = parse(MINIMAL).unwrap();
        assert_eq!(s.data_dir, PathBuf::from("/home/u/.muliao/data"));
        assert_eq!(s.log_file, Some(PathBuf::from("/home/u/.muliao/muliao.log")));
        assert_eq!(s.db_path(), PathBuf::from("/home/u/.muliao/data/muliao.db"));
        assert_eq!(s.timezone, chrono_tz::Asia::Shanghai);
        assert_eq!(s.runtime.precheck_timeout_seconds, 60.0);
        assert_eq!(s.runtime.compaction_threshold_tokens, 100_000);
        assert_eq!((demo.count, demo.api_key.expose()), (2, "sk-xyz"));
        assert!(s.private_files().contains(&PathBuf::from("/home/u/.muliao/config.toml.prev")));
        // 密钥不进 Debug 输出
        assert!(!format!("{s:?} {demo:?}").contains("sk-xyz"));
    }

    #[test]
    fn unknown_keys_and_bad_values_are_errors() {
        let typo = MINIMAL.replace("precheck_timeout_seconds", "precheck_timeout");
        assert!(parse(&typo).unwrap_err().to_string().contains("precheck_timeout"));
        let bad_tz = MINIMAL.replace("Asia/Shanghai", "Mars/Base");
        assert!(parse(&bad_tz).unwrap_err().to_string().contains("timezone"));
        // 段里的未知键报错，行号是在整个文件里的
        let typo = MINIMAL.replace("count = 2", "count = 2\ncont = 3");
        let err = parse(&typo).unwrap_err().to_string();
        assert!(err.contains("cont") && err.contains("line 9"), "{err}");
        let numeric = MINIMAL.replace("\"sk-xyz\"", "987654321");
        let err = parse(&numeric).unwrap_err().to_string();
        assert!(err.contains("must be a string") && !err.contains("987654321"), "{err}");
        let zero = MINIMAL.replace("count = 2", "count = 0");
        assert!(parse(&zero).unwrap_err().to_string().contains("demo.count must be at least 1"));
        // 谁也没取走的段（拼错的段名）是平台这一层的未知键
        let stray = format!("{MINIMAL}[demmo]\nx = 1\n");
        assert!(parse(&stray).unwrap_err().to_string().contains("demmo"));
        let missing = MINIMAL.replace("[demo]\napi_key = \"sk-xyz\"\ncount = 2\n", "");
        assert!(parse(&missing).unwrap_err().to_string().contains("missing section [demo]"));
        let broken = MINIMAL.replace("[vault]", "[vault");
        assert!(parse(&broken).unwrap_err().to_string().contains("line 4"));
    }
}
