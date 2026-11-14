//! 各种资源文件的格式：每种一个模块，读树按它的 `load` 读一项，写入口按它的 `vet` 校验要写成的全文（经
//! [`crate::kinds::StoredKind::load`]、[`crate::kinds::StoredKind::vet`] 分派）。这里是两者共用的输入与输出，不依赖任何种类。

pub mod agent_file;
pub mod mcp;
pub mod memory;
pub mod secret;
pub mod skill;

use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};

use crate::kinds::Content;

/// load 读出的一项：内容、修改时间，以及照常加载但要报出的问题（记忆超长）。
#[derive(Debug, Clone)]
pub struct Loaded {
    pub content: Content,
    pub updated_at: DateTime<Utc>,
    pub warning: Option<String>,
}

/// 写入口交给 vet 的一份文件：属于哪一项资源、要写成什么。
#[derive(Debug, Clone)]
pub struct Draft<'a> {
    /// 这一项资源的文件（skill 是它的目录）
    pub item: PathBuf,
    pub name: &'a str,
    pub text: &'a str,
}

/// 文件的修改时间（UTC）。
pub fn modified(path: &Path) -> std::io::Result<DateTime<Utc>> {
    Ok(DateTime::<Utc>::from(std::fs::metadata(path)?.modified()?))
}

/// 读资源文本文件，换行统一成 `\n`；读不出、不是 UTF-8 时的说明
/// （读树记成问题）。
pub(crate) fn read(path: &Path) -> Result<String, String> {
    muliao_platform::fs::read_text(path)
        .map(|t| match lf(&t) {
            std::borrow::Cow::Borrowed(_) => t,
            std::borrow::Cow::Owned(o) => o,
        })
        .map_err(|e| match e.kind() {
            std::io::ErrorKind::InvalidData => e.to_string(),
            _ => format!("cannot read {}: {e}", path.display()),
        })
}

/// 换行统一成 `\n`（[`read`] 读出的样子）。
pub(crate) fn lf(text: &str) -> std::borrow::Cow<'_, str> {
    if text.contains('\r') { text.replace("\r\n", "\n").replace('\r', "\n").into() } else { text.into() }
}

/// Python 的 str.splitlines：这些字符都算换行（\r\n 算一个）。
pub(crate) fn split_lines(text: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        if matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            out.push(&text[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|(_, n)| *n == '\n') {
                chars.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < text.len() {
        out.push(&text[start..]);
    }
    out
}

/// 读文本，非 UTF-8 的字节换成 U+FFFD（只为比对：读坏了的也要能整个覆盖修好）；不存在为 None。
pub(crate) fn read_lossy(path: &Path) -> Option<String> {
    std::fs::read(path).ok().map(|b| String::from_utf8_lossy(&b).into_owned())
}
