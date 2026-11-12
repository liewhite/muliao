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

/// NFC 规范化：同一个字的不同编码形式视为同一个名称（macOS 的文件名可能是 NFD）。
pub fn nfc(name: &str) -> String {
    name.nfc().collect()
}

/// agent 名称只用于展示（标识用 id），所以只要求：去首尾空白后非空、不超过 64 个字符、不含控制字符（Unicode 类别
/// C*）。NFC 规范化，使同一个字的不同编码形式被视为同一个名称（名称唯一）。
pub fn normalize_agent_name(value: &str) -> Result<String, DomainError> {
    let name = nfc(value).trim().to_owned();
    let len = name.chars().count();
    if name.is_empty() || len > AGENT_NAME_MAX {
        return Err(DomainError::invalid(format!("agent name must be 1-{AGENT_NAME_MAX} characters, got {len}")));
    }
    if name.chars().any(|c| c.general_category_group() == GeneralCategoryGroup::Other) {
        return Err(DomainError::invalid(format!("agent name {value:?} must not contain control characters")));
    }
    Ok(name)
}

/// 给 agent 起名或改名时的名称：[`normalize_agent_name`]，不能是 [`USER`]（模型按名称指代 agent 的地方用它指用户）；
/// 它还是资源目录里的目录名，所以要能当一个文件名：不含 `/`、`\`，不以 `.` 开头（隐藏目录不读），不是 platform。
/// 只在起名时拦：按名称查找时照常规范化。
pub fn new_agent_name(value: &str) -> Result<String, DomainError> {
    let name = normalize_agent_name(value)?;
    if name == USER {
        return Err(DomainError::invalid(format!(
            "agent name {USER:?} is reserved: it refers to the user (e.g. as issue assignee)"
        )));
    }
    if name.contains('/') || name.contains('\\') || name.starts_with('.') || name == PLATFORM {
        return Err(DomainError::invalid(format!(
            "agent name {name:?} must not contain '/' or '\\', start with '.', or be {PLATFORM:?}: it is a directory \
             name in the asset tree"
        )));
    }
    Ok(name)
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
        assert_eq!(new_agent_name("  交易 ").unwrap(), "交易");
        assert!(new_agent_name("user").is_err());
        assert!(new_agent_name("platform").is_err());
        assert!(new_agent_name(".x").is_err());
        assert!(new_agent_name("a/b").is_err());
        assert!(new_agent_name("a\u{200d}b").is_err(), "format characters are C*");
        assert!(new_agent_name(&"x".repeat(65)).is_err());
        // NFD 与 NFC 是同一个名字
        assert_eq!(nfc("e\u{301}"), "\u{e9}");
        assert!(validate_slug("skill", "a-b_9").is_ok());
        assert!(validate_slug("skill", "-a").is_err());
        assert!(validate_slug("skill", "A").is_err());
        assert!(validate_slug("skill", &"a".repeat(65)).is_err());
    }
}
