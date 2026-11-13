//! 记忆文件（`platform/memory/<名>.md`、`<agent 名>/memory.md`）的格式：读出的正文、存下的样子、长度上限。

use std::path::Path;

use super::{Draft, Loaded, modified, read};
use crate::kinds::Content;

/// 记忆的内容：正文（去掉首尾空白）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemoryText {
    pub text: String,
}

/// 读树：超过上限的照常加载（截断更糟），但报出来让它缩短。
pub fn load(path: &Path, _name: &str, memory_max_chars: usize) -> Result<Loaded, String> {
    let text = text_of(path)?;
    let warning = over_limit(&text, memory_max_chars);
    Ok(Loaded {
        content: Content::Memory(MemoryText { text }),
        updated_at: modified(path).map_err(|e| e.to_string())?,
        warning,
    })
}

/// 写入口：实际存下的全文。
pub fn vet(draft: &Draft<'_>) -> Result<String, String> {
    let old = if draft.item.is_file() { super::read_lossy(&draft.item) } else { None };
    stored(draft.text, old.as_deref(), draft.memory_max_chars)
}

/// 文件里的正文（指令里显示的、界面编辑的都是它）。
pub fn text_of(path: &Path) -> Result<String, String> {
    Ok(body_of(&read(path)?))
}

/// 读出的原文对应的正文：去掉首尾空白。
pub fn body_of(raw: &str) -> String {
    raw.trim().to_owned()
}

/// 正文超过上限时的问题说明。
pub fn over_limit(text: &str, limit: usize) -> Option<String> {
    let n = text.chars().count();
    (n > limit).then(|| format!("memory is {n} chars, over the limit of {limit}; condense it"))
}

/// 要写成 text 时文件里实际存下的全文：规整空白（[`tidy`]）后加结尾换行。规整后超过上限且比原来（old，新建为 None）长
/// 才拒绝：已经超长的（shell 追加、上限调低）可以一段段删短，不必一次删够。
pub fn stored(text: &str, old: Option<&str>, limit: usize) -> Result<String, String> {
    let content = tidy(text);
    let previous = old.map(|o| tidy(o).chars().count()).unwrap_or(0);
    if content.chars().count() > previous
        && let Some(problem) = over_limit(&content, limit)
    {
        return Err(format!("after tidying whitespace the {problem}"));
    }
    Ok(if content.is_empty() { String::new() } else { content + "\n" })
}

/// 每行去掉行尾空白，连续空行合并成一个，去掉首尾空行，不留结尾换行：存下的就是指令里显示的样子（#361）。
pub fn tidy(content: &str) -> String {
    let mut lines: Vec<&str> = Vec::new();
    for line in super::split_lines(content) {
        let line = line.trim_end();
        if !line.is_empty() || lines.last().is_some_and(|l| !l.is_empty()) {
            lines.push(line);
        }
    }
    while lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tidy_and_limit() {
        assert_eq!(tidy("\n\na  \n\n\n b\r\n\n"), "a\n\n b");
        assert_eq!(tidy("a\u{2028}b\rc\x0c"), "a\nb\nc");
        assert_eq!(stored("x\n", None, 10).unwrap(), "x\n");
        assert_eq!(stored("  \n", None, 10).unwrap(), "");
        assert!(stored("abcdefghijk", None, 10).is_err());
        // 已经超长的可以一段段删短
        assert!(stored("abcdefghijk", Some("abcdefghijkl"), 10).is_ok());
    }
}
