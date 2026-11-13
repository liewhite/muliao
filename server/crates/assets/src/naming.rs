//! agent 的标识与各种名字的规则：agent id、指代用户的 [`USER`]、agent 名、slug，以及资源目录里平台的目录名。

use unicode_normalization::UnicodeNormalization;
use unicode_properties::{GeneralCategoryGroup, UnicodeGeneralCategory};

use muliao_platform::errors::DomainError;

/// agent id：12 位小写十六进制，短到模型能可靠复制，用作主键、目录名与工具参数
const ID_LENGTH: usize = 12;

pub fn new_agent_id() -> String {
    uuid::Uuid::new_v4().simple().to_string()[..ID_LENGTH].to_owned()
}

/// 全系统指代用户的那个值：做事的人（issue 时间线等）、issue 的指派人等处与 agent id 并列出现。不是 12 位十六进制，
/// 不会与 agent id 相同；agent 名称也不能取它（[`new_agent_name`]）。
pub const USER: &str = "user";

/// 资源目录里平台的目录名：agent 不能取
pub const PLATFORM: &str = "platform";

pub const AGENT_NAME_MAX: usize = 64;

/// agent id 的形状（12 位小写十六进制）：access.toml 的 `[agents.<id>]` 节名按它校验。
pub fn is_agent_id(value: &str) -> bool {
    value.len() == ID_LENGTH && value.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// agent 名：去首尾空白、NFC 规范化后的写法，非空、不超过 64 个字符、不含控制字符（Unicode 类别 C*）。
///
/// 两个名字相等（含哈希、排序）按 [`AgentName::key`]：同一个字的不同编码（NFC / NFD）、只差大小写的，都是同一个名字。
/// 要知道写法变了没有（如改名只改了大小写）用 [`AgentName::as_str`] 比。
/// 名字也是资源目录里的目录名，macOS 的文件系统不分大小写与编码形式，所以唯一性按这个键算（数据库 `name_key` 列）。
#[derive(Clone)]
pub struct AgentName {
    text: String,
    key: String,
}

impl AgentName {
    /// 按名字查找时的解析：只做规范化与形式检查，不查起名的额外限制（[`AgentName::new`]）。
    pub fn parse(value: &str) -> Result<Self, DomainError> {
        let name: String = value.nfc().collect::<String>().trim().to_owned();
        let len = name.chars().count();
        if name.is_empty() || len > AGENT_NAME_MAX {
            return Err(DomainError::invalid(format!("agent name must be 1-{AGENT_NAME_MAX} characters, got {len}")));
        }
        if name.chars().any(|c| c.general_category_group() == GeneralCategoryGroup::Other) {
            return Err(DomainError::invalid(format!("agent name {value:?} must not contain control characters")));
        }
        Ok(Self::stored(name))
    }

    /// 给 agent 起名或改名：[`AgentName::parse`]，再加上不能是 [`USER`]（模型按名称指代 agent 的地方用它指用户）；它还是
    /// 资源目录里的目录名，所以要能当一个文件名：不含 `/`、`\`，不以 `.` 开头（隐藏目录不读），不是 platform。
    pub fn new(value: &str) -> Result<Self, DomainError> {
        let name = Self::parse(value)?;
        if name.key == USER {
            return Err(DomainError::invalid(format!(
                "agent name {USER:?} is reserved: it refers to the user (e.g. as issue assignee)"
            )));
        }
        let t = &name.text;
        if t.contains('/') || t.contains('\\') || t.starts_with('.') || name.key == PLATFORM {
            return Err(DomainError::invalid(format!(
                "agent name {t:?} must not contain '/' or '\\', start with '.', or be {PLATFORM:?}: it is a directory \
                 name in the asset tree"
            )));
        }
        Ok(name)
    }

    /// 数据库里存着的名字（存入前已经过 [`AgentName::new`]）。
    pub(crate) fn stored(text: String) -> Self {
        let key = caseless::default_case_fold_str(&text);
        Self { text, key }
    }

    pub fn as_str(&self) -> &str {
        &self.text
    }

    /// 判断是不是同一个名字用的键（大小写折叠）。
    pub fn key(&self) -> &str {
        &self.key
    }
}

impl PartialEq for AgentName {
    fn eq(&self, other: &Self) -> bool {
        self.key == other.key
    }
}

impl Eq for AgentName {}

impl std::hash::Hash for AgentName {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        self.key.hash(state);
    }
}

impl PartialOrd for AgentName {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for AgentName {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.key.cmp(&other.key)
    }
}

impl std::fmt::Display for AgentName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.text)
    }
}

/// 和字符串一样打印（报错信息里 `{name:?}`）
impl std::fmt::Debug for AgentName {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        std::fmt::Debug::fmt(&self.text, f)
    }
}

impl AsRef<str> for AgentName {
    fn as_ref(&self) -> &str {
        &self.text
    }
}

impl AsRef<std::path::Path> for AgentName {
    fn as_ref(&self) -> &std::path::Path {
        std::path::Path::new(&self.text)
    }
}

impl serde::Serialize for AgentName {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.text)
    }
}

impl rusqlite::types::FromSql for AgentName {
    fn column_result(value: rusqlite::types::ValueRef<'_>) -> rusqlite::types::FromSqlResult<Self> {
        String::column_result(value).map(Self::stored)
    }
}

/// skill、MCP server、平台记忆与角色的名字：会出现在目录名和工具名前缀里。`^[a-z0-9][a-z0-9_-]{0,63}$`
pub fn validate_slug(what: &str, value: &str) -> Result<(), DomainError> {
    let b = value.as_bytes();
    let ok = !b.is_empty()
        && b.len() <= 64
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b[1..].iter().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_' || *c == b'-');
    if ok {
        Ok(())
    } else {
        Err(DomainError::invalid(format!("invalid {what} name {value:?}: must match ^[a-z0-9][a-z0-9_-]{{0,63}}$")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names() {
        let id = new_agent_id();
        assert_eq!(id.len(), 12);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
        assert_eq!(AgentName::new("  交易 ").unwrap().as_str(), "交易");
        assert!(AgentName::new("user").is_err());
        assert!(AgentName::new("Platform").is_err());
        assert!(AgentName::new(".x").is_err());
        assert!(AgentName::new("a/b").is_err());
        assert!(AgentName::new("a\u{200d}b").is_err(), "format characters are C*");
        assert!(AgentName::new(&"x".repeat(65)).is_err());
        // NFD 与 NFC、只差大小写的，是同一个名字；写法保留 NFC
        let nfd = AgentName::parse("Cafe\u{301}").unwrap();
        assert_eq!(nfd.as_str(), "Caf\u{e9}");
        assert_eq!(nfd, AgentName::parse("caf\u{e9}").unwrap());
        assert!(is_agent_id(&id) && !is_agent_id("muliao") && !is_agent_id("143CD08D29B0"));
        assert!(validate_slug("skill", "a-b_9").is_ok());
        assert!(validate_slug("skill", "-a").is_err());
        assert!(validate_slug("skill", "A").is_err());
        assert!(validate_slug("skill", &"a".repeat(65)).is_err());
    }
}
