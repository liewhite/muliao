//! 文本的规则（纯函数），文件工具用：行的切分（同 Python 的 `str.splitlines`，read_file 的行号与 edit_file 的行号区间
//! 必须是同一种分法）、带行号展示（`cat -n` 格式）、找恰好出现一次的片段与找不到时的提示、按位置替换。
//!
//! 位置一律是字节下标（Python 版是字符下标：两边只在同一份文本里互相换算，结果一样）；交给模型的长度上限按字符数算。

use muliao_platform::errors::DomainError;

/// 带行号展示时单行的上限（字符）：一行压缩过的 JS/JSON 能吃掉整个输出预算
pub const MAX_LINE_CHARS: usize = 2000;

/// 一行在文本里的范围（字节）：内容是 `[start, end)`，连同行尾的分隔符是 `[start, next)`。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Line {
    pub start: usize,
    pub end: usize,
    pub next: usize,
}

/// 按 Python `str.splitlines` 的规则切行：`\n`、`\r`、`\r\n`、`\x0b`、`\x0c`、`\x1c`–`\x1e`、`\x85`、`\u2028`、`\u2029`
/// 都是行尾；末尾的行尾不再多出一个空行。
pub fn lines_of(text: &str) -> Vec<Line> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let sep_len = match c {
            '\r' => match chars.peek() {
                Some((_, '\n')) => {
                    chars.next();
                    2
                }
                _ => 1,
            },
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}' => c.len_utf8(),
            _ => continue,
        };
        out.push(Line { start, end: i, next: i + sep_len });
        start = i + sep_len;
    }
    if start < text.len() {
        out.push(Line { start, end: text.len(), next: text.len() });
    }
    out
}

/// `str.splitlines()`：各行的内容。
pub fn split_lines(text: &str) -> Vec<&str> {
    lines_of(text).into_iter().map(|l| &text[l.start..l.end]).collect()
}

/// `str.splitlines(keepends=True)`：各行连同行尾。
pub fn split_lines_keep(text: &str) -> Vec<&str> {
    lines_of(text).into_iter().map(|l| &text[l.start..l.next]).collect()
}

/// text 以行尾结尾（按 [`lines_of`] 的规则）。
pub fn ends_line(text: &str) -> bool {
    lines_of(text).last().is_some_and(|l| l.end < l.next)
}

/// 各行开头的位置，末尾再加上全文长度（第 i 行是 `[starts[i], starts[i+1])`）。
pub fn line_starts(text: &str) -> Vec<usize> {
    let mut starts = vec![0];
    starts.extend(lines_of(text).into_iter().map(|l| l.next));
    starts
}

/// 字符数（交给模型的长度都按它算）。
pub fn chars(text: &str) -> usize {
    text.chars().count()
}

/// 过长的行截断（[`MAX_LINE_CHARS`]）。
pub fn clip(line: &str) -> String {
    match line.char_indices().nth(MAX_LINE_CHARS) {
        None => line.to_owned(),
        Some((cut, _)) => {
            format!("{}…[line truncated: {} more chars]", &line[..cut], chars(&line[cut..]))
        }
    }
}

/// `lines[start..end]` 按 `cat -n` 的格式：行号（1 起算）右对齐占 6 列，后跟一个制表符；过长的行截断。带行号的内容
/// （read_file、edit_file 的回显与报错）都用它，模型拿来就认得。
pub fn numbered(lines: &[&str], start: usize, end: usize) -> String {
    lines[start..end]
        .iter()
        .enumerate()
        .map(|(k, line)| format!("{:6}\t{}", start + k + 1, clip(line)))
        .collect::<Vec<_>>()
        .join("\n")
}

/// 报错里「在哪几行」的列表，最多列 20 个。
pub fn listed(line_numbers: &[usize]) -> String {
    let shown: Vec<String> = line_numbers.iter().take(20).map(|n| n.to_string()).collect();
    let more = if line_numbers.len() > 20 { " …" } else { "" };
    format!("{}{more}", shown.join(", "))
}

/// old 在 current 里每一处的起点（不重叠）；old 为空时没有。
pub fn occurrences(current: &str, old: &str) -> Vec<usize> {
    if old.is_empty() { Vec::new() } else { current.match_indices(old).map(|(i, _)| i).collect() }
}

/// old 恰好出现一次时它的起点；否则报错，带上能改对的线索。
pub fn locate_once(current: &str, old: &str, what: &str) -> Result<usize, DomainError> {
    let at = occurrences(current, old);
    if at.len() != 1 {
        return Err(DomainError::invalid(miss(
            current,
            old,
            at.len(),
            &format!("old text must occur exactly once in {what}"),
        )));
    }
    Ok(at[0])
}

/// 替换失败的说明，带上能让模型改对的线索：多处时在哪几行，没有时最接近的是哪里。
pub fn miss(current: &str, old: &str, count: usize, rule: &str) -> String {
    let message = format!("{rule}, found {count} occurrences");
    if old.is_empty() {
        return message;
    }
    if count > 1 {
        let mut lines: Vec<usize> =
            occurrences(current, old).into_iter().map(|i| current[..i].matches('\n').count() + 1).collect();
        lines.dedup();
        return format!("{message} (at lines {}); include more surrounding text to make it unique", listed(&lines));
    }
    let wanted: Vec<&str> = split_lines(old).into_iter().map(str::trim).collect();
    let have: Vec<&str> = split_lines(current).into_iter().map(str::trim).collect();
    let n = wanted.len();
    if n <= have.len() {
        for i in 0..=have.len() - n {
            if have[i..i + n] == wanted[..] {
                return format!(
                    "{message}; lines {}-{} match it except for whitespace/indentation: copy that text exactly as it \
                     is now",
                    i + 1,
                    i + n
                );
            }
        }
    }
    let first = wanted.iter().find(|w| !w.is_empty()).copied().unwrap_or("");
    let at: Vec<String> = have
        .iter()
        .enumerate()
        .filter(|(_, line)| !first.is_empty() && **line == first)
        .map(|(i, _)| (i + 1).to_string())
        .collect();
    if !at.is_empty() {
        let shown: Vec<String> = at.into_iter().take(10).collect();
        return format!(
            "{message}; its first non-blank line is at line {} but what follows differs: re-read that part and copy \
             it exactly",
            shown.join(", ")
        );
    }
    format!("{message}; not even its first non-blank line occurs: re-read the current text before editing")
}

/// 把文本的 `[start, end)` 换成 new。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Splice {
    pub start: usize,
    pub end: usize,
    pub new: String,
}

/// 按顺序排列、互不重叠的各处替换做出新文本。
pub fn splice(text: &str, places: &[Splice]) -> String {
    let mut out = String::with_capacity(text.len());
    let mut at = 0;
    for p in places {
        out.push_str(&text[at..p.start]);
        out.push_str(&p.new);
        at = p.end;
    }
    out.push_str(&text[at..]);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_lines_like_python() {
        assert_eq!(split_lines("a\nb\r\nc\rd\x0be\u{2028}f"), ["a", "b", "c", "d", "e", "f"]);
        assert_eq!(split_lines("a\n"), ["a"]);
        assert_eq!(split_lines("a\n\n"), ["a", ""]);
        assert!(split_lines("").is_empty());
        assert_eq!(split_lines_keep("a\r\nb"), ["a\r\n", "b"]);
        assert!(ends_line("a\r") && !ends_line("a") && !ends_line(""));
        assert_eq!(line_starts("ab\ncd\n"), [0, 3, 6]);
    }

    #[test]
    fn numbers_lines_like_cat_n() {
        let lines = ["x", "y"];
        assert_eq!(numbered(&lines, 0, 2), "     1\tx\n     2\ty");
        let long = "é".repeat(MAX_LINE_CHARS + 3);
        assert!(numbered(&[long.as_str()], 0, 1).ends_with("…[line truncated: 3 more chars]"));
    }

    #[test]
    fn misses_point_at_where_to_look() {
        let text = "a\n  b\nc\nb\n";
        assert_eq!(
            miss(text, "x", 0, "r"),
            "r, found 0 occurrences; not even its first non-blank line occurs: re-read the current text before editing"
        );
        assert!(miss(text, "b", 2, "r").contains("(at lines 2, 4)"));
        assert!(miss(text, "a\nb", 0, "r").contains("lines 1-2 match it except for whitespace"));
        assert!(miss(text, "c\nz", 0, "r").contains("its first non-blank line is at line 3"));
        assert_eq!(locate_once("abc", "b", "f").unwrap(), 1);
    }
}
