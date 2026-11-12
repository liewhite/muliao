//! secret 文件 `secrets/<KEY>.toml` 的格式与 key 的语法：
//!
//! ```toml
//! description = "…"
//! ciphertext = "gAAAA…"   # Fernet 密文（vault_set 加密），不能手写
//! allow_shell = false     # 可省略，缺省 true：false 时 agent 自写的命令（run_shell、precheck、stream）不能注入它，
//!                         # 只有 skill 里的脚本（skill_run）与 MCP 配置能用（#528）
//! ```
//! key 语法也用于 `${vault:KEY}` 引用（[`refs`]、[`fill`]）。

use std::collections::BTreeMap;
use std::fmt;
use std::path::Path;

use super::{Draft, Loaded, modified, read};
use crate::kinds::Content;

const KEY_MAX: usize = 100;

fn key_syntax(key: &str) -> bool {
    let mut chars = key.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphabetic() || c == '_')
        && chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

/// secret key 的规则：`[A-Za-z_][A-Za-z0-9_]*`，不超过 100 个字符。长度上限只在存储时查：引用了超长 key 的模板照样认
/// 作引用，解析时按「不存在」报错，而不是当成字面文本原样代入。
pub fn validate_key(key: &str) -> Result<(), String> {
    if key_syntax(key) && key.len() <= KEY_MAX {
        Ok(())
    } else {
        Err(format!(
            "invalid secret key {key:?}: must match ^[A-Za-z_][A-Za-z0-9_]*$ and be at most {KEY_MAX} characters"
        ))
    }
}

const REF_OPEN: &str = "${vault:";

/// 模板里的每一处 `${vault:KEY}`：(起止字节位置, KEY)。
fn ref_spans(template: &str) -> Vec<(usize, usize, &str)> {
    let mut out = Vec::new();
    let mut from = 0;
    while let Some(i) = template[from..].find(REF_OPEN) {
        let start = from + i;
        let key_start = start + REF_OPEN.len();
        match template[key_start..].find('}') {
            Some(j) if key_syntax(&template[key_start..key_start + j]) => {
                let end = key_start + j + 1;
                out.push((start, end, &template[key_start..key_start + j]));
                from = end;
            }
            _ => from = key_start,
        }
    }
    out
}

/// 模板里引用的 key（按出现顺序，不去重）。
pub fn refs(template: &str) -> Vec<&str> {
    ref_spans(template).into_iter().map(|(_, _, k)| k).collect()
}

/// 把模板里的 `${vault:KEY}` 换成 values 里的明文（values 须含全部引用，通常来自解析出的明文；缺了是调用方的 bug）。
pub fn fill(template: &str, values: &BTreeMap<String, String>) -> String {
    let mut out = String::with_capacity(template.len());
    let mut last = 0;
    for (start, end, key) in ref_spans(template) {
        out.push_str(&template[last..start]);
        out.push_str(values.get(key).unwrap_or_else(|| panic!("secret {key:?} was not resolved before fill")));
        last = end;
    }
    out.push_str(&template[last..]);
    out
}

#[derive(Clone, PartialEq, Eq)]
pub struct SecretFile {
    pub description: String,
    /// Fernet 密文；不出这个领域
    pub(crate) ciphertext: String,
    /// agent 自写的命令能不能注入它（见模块说明）
    pub allow_shell: bool,
}

impl fmt::Debug for SecretFile {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SecretFile")
            .field("description", &self.description)
            .field("allow_shell", &self.allow_shell)
            .finish()
    }
}

/// 不合规则时报错。密文能不能解开在用到时才知道（解不开报出是哪个 key）。
pub fn parse(key: &str, text: &str) -> Result<SecretFile, String> {
    validate_key(key)?;
    let data: toml::Table = text.parse().map_err(|e: toml::de::Error| format!("cannot parse: {}", e.message()))?;
    let shape = "a secret file has only 'description' (string), 'ciphertext' (set by vault_set; never write it by \
                 hand) and optionally 'allow_shell' (true or false, default true)";
    if data.keys().any(|k| !matches!(k.as_str(), "description" | "ciphertext" | "allow_shell")) {
        return Err(shape.into());
    }
    let description = match data.get("description") {
        None => String::new(),
        Some(toml::Value::String(s)) => s.clone(),
        Some(_) => return Err(shape.into()),
    };
    let ciphertext = match data.get("ciphertext") {
        Some(toml::Value::String(s)) if !s.is_empty() => s.clone(),
        _ => return Err(shape.into()),
    };
    let allow_shell = match data.get("allow_shell") {
        None => true,
        Some(toml::Value::Boolean(b)) => *b,
        Some(_) => return Err(shape.into()),
    };
    Ok(SecretFile { description, ciphertext, allow_shell })
}

pub fn load(path: &Path, name: &str, _memory_max_chars: usize) -> Result<Loaded, String> {
    let file = parse(name, &read(path)?)?;
    Ok(Loaded { content: Content::Secret(file), updated_at: modified(path).map_err(|e| e.to_string())?, warning: None })
}

const HINT: &str = "secret values are encrypted by the platform: store them with vault_set";

/// 密文只能由 vault_set 加密写入：已有的 secret 可以只改 description 与 allow_shell（密文原样不动），不能手写新的。
pub fn vet(draft: &Draft<'_>) -> Result<String, String> {
    if !draft.item.is_file() {
        return Err(format!("new secrets are not written by hand; {HINT}"));
    }
    let new = parse(draft.name, draft.text).map_err(|e| format!("{e}; {HINT}"))?;
    let old = read(&draft.item).and_then(|t| parse(draft.name, &t)).map_err(|e| format!("{e}; {HINT}"))?;
    if new.ciphertext != old.ciphertext {
        return Err(format!(
            "only the description and allow_shell may be edited by hand, the ciphertext must stay; {HINT}"
        ));
    }
    Ok(draft.text.to_owned())
}

/// 写进文件的全文（vault_set）。
pub fn render(secret: &SecretFile) -> String {
    let mut doc = toml_edit::DocumentMut::new();
    doc["description"] = toml_edit::value(secret.description.as_str());
    doc["ciphertext"] = toml_edit::value(secret.ciphertext.as_str());
    doc["allow_shell"] = toml_edit::value(secret.allow_shell);
    doc.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn refs_and_fill() {
        let t = "a ${vault:K1} b ${vault:bad-key} ${vault:_x}${vault:";
        assert_eq!(refs(t), vec!["K1", "_x"]);
        let values = BTreeMap::from([("K1".to_owned(), "v1".to_owned()), ("_x".to_owned(), "v2".to_owned())]);
        assert_eq!(fill(t, &values), "a v1 b ${vault:bad-key} v2${vault:");
    }

    #[test]
    fn parse_rules() {
        let f = parse("K", "description = \"d\"\nciphertext = \"c\"\n").unwrap();
        assert!(f.allow_shell);
        assert_eq!(parse("K", &render(&f)).unwrap(), f);
        assert!(parse("K", "ciphertext = \"\"").is_err());
        assert!(parse("K", "ciphertext = \"c\"\nvalue = 1").is_err());
        assert!(parse("1K", "ciphertext = \"c\"").is_err());
        assert!(validate_key(&"K".repeat(101)).is_err());
    }
}
