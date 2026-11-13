//! MCP server 的连接配置（`mcp/<名>.json`）：传输方式与字段的搭配在这里校验，读写同一条规则。
//!
//! ```json
//! {"transport": "stdio", "command": "npx", "args": ["-y", "x"], "env": {"K": "${vault:KEY}"}, "enabled": false}
//! ```
//! 与传输方式无关的字段可以省略；`enabled` 缺省为真。env / headers / args / url 的值可以含 `${vault:KEY}`。

use std::collections::BTreeMap;
use std::path::Path;

use serde::{Deserialize, Serialize};

use super::{Draft, Loaded, modified, read};
use crate::kinds::Content;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "lowercase")]
pub enum McpTransport {
    Stdio,
    Http,
}

/// 一个 MCP server 的连接配置：每种传输方式只带自己的字段（外部输入经 [`McpConfig::new`] 或读文件在边界解析一次）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "transport", rename_all = "lowercase")]
pub enum McpConfig {
    Stdio { command: String, args: Vec<String>, env: BTreeMap<String, String> },
    Http { url: String, headers: BTreeMap<String, String> },
}

impl McpConfig {
    /// 调用方（用户、agent、文件）按扁平字段给出的配置；不合传输方式的搭配是输入错误。空串与没给一样。
    pub fn new(
        transport: McpTransport,
        command: Option<String>,
        args: Vec<String>,
        url: Option<String>,
        env: BTreeMap<String, String>,
        headers: BTreeMap<String, String>,
    ) -> Result<Self, String> {
        let bad = |e: &str| format!("invalid MCP server config: {e}");
        let given = |v: Option<String>| v.filter(|v| !v.is_empty());
        match (transport, given(command), given(url)) {
            (McpTransport::Stdio, Some(command), None) if headers.is_empty() => {
                Ok(McpConfig::Stdio { command, args, env })
            }
            (McpTransport::Stdio, ..) => {
                Err(bad("stdio MCP server requires 'command' and must not set 'url' or 'headers'"))
            }
            (McpTransport::Http, None, Some(url)) if args.is_empty() && env.is_empty() => {
                if !(url.starts_with("http://") || url.starts_with("https://")) {
                    return Err(bad("MCP url must start with http:// or https://"));
                }
                Ok(McpConfig::Http { url, headers })
            }
            (McpTransport::Http, ..) => {
                Err(bad("http MCP server requires 'url' and must not set 'command', 'args' or 'env'"))
            }
        }
    }

    pub fn transport(&self) -> McpTransport {
        match self {
            McpConfig::Stdio { .. } => McpTransport::Stdio,
            McpConfig::Http { .. } => McpTransport::Http,
        }
    }

    /// 可以含 `${vault:KEY}` 的值（args、env、url、headers）。
    pub fn templates(&self) -> Vec<&str> {
        match self {
            McpConfig::Stdio { args, env, .. } => args.iter().chain(env.values()).map(String::as_str).collect(),
            McpConfig::Http { url, headers } => {
                std::iter::once(url.as_str()).chain(headers.values().map(String::as_str)).collect()
            }
        }
    }
}

/// MCP server 的内容：配置与是否启用。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpFile {
    pub config: McpConfig,
    pub enabled: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Raw {
    transport: McpTransport,
    #[serde(default)]
    command: Option<String>,
    #[serde(default)]
    args: Vec<String>,
    #[serde(default)]
    url: Option<String>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    headers: BTreeMap<String, String>,
    #[serde(default = "enabled_default")]
    enabled: bool,
}

fn enabled_default() -> bool {
    true
}

/// `mcp/<名>.json` 的内容。
pub fn parse(text: &str) -> Result<McpFile, String> {
    let value: serde_json::Value = serde_json::from_str(text).map_err(|e| format!("not valid JSON: {e}"))?;
    if !value.is_object() {
        return Err("must be a JSON object".into());
    }
    if value.get("enabled").is_some_and(|e| !e.is_boolean()) {
        return Err("'enabled' must be true or false".into());
    }
    let raw: Raw = serde_json::from_value(value).map_err(|e| format!("invalid MCP server config: {e}"))?;
    let config = McpConfig::new(raw.transport, raw.command, raw.args, raw.url, raw.env, raw.headers)?;
    Ok(McpFile { config, enabled: raw.enabled })
}

/// [`parse`] 的反面：写进文件的文本（停用时才写 enabled）。
pub fn render(config: &McpConfig, enabled: bool) -> String {
    let mut value = serde_json::to_value(config).expect("MCP config serializes");
    if !enabled {
        value["enabled"] = serde_json::Value::Bool(false);
    }
    serde_json::to_string_pretty(&value).expect("JSON value serializes") + "\n"
}

pub fn load(path: &Path, _name: &str, _memory_max_chars: usize) -> Result<Loaded, String> {
    let file = parse(&read(path)?)?;
    Ok(Loaded { content: Content::Mcp(file), updated_at: modified(path).map_err(|e| e.to_string())?, warning: None })
}

pub fn vet(draft: &Draft<'_>) -> Result<String, String> {
    parse(draft.text)?;
    Ok(draft.text.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_and_render() {
        let f = parse(r#"{"transport": "stdio", "command": "npx", "env": {"K": "${vault:A}"}}"#).unwrap();
        assert!(f.enabled);
        assert_eq!(parse(&render(&f.config, false)).unwrap(), McpFile { config: f.config.clone(), enabled: false });
        assert!(parse(r#"{"transport": "stdio"}"#).is_err());
        assert!(parse(r#"{"transport": "http", "url": "ftp://x"}"#).is_err());
        assert!(parse(r#"{"transport": "http", "url": "https://x", "extra": 1}"#).is_err());
        assert!(parse(r#"{"transport": "http", "url": "https://x", "enabled": "no"}"#).is_err());
        assert!(parse(r#"[]"#).is_err());
    }
}
