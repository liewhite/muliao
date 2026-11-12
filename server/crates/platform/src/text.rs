//! 文本的小工具（各领域共用）。

/// 超过 `limit` 个字符时截断，并注明还剩多少字符没给（按字符数，不按字节，不会切坏多字节字符）。
pub fn truncate(text: &str, limit: usize) -> String {
    match text.char_indices().nth(limit) {
        None => text.to_string(),
        Some((cut, _)) => {
            let rest = text[cut..].chars().count();
            format!("{}…[truncated: {rest} more chars]", &text[..cut])
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncates_by_chars() {
        assert_eq!(truncate("abc", 3), "abc");
        assert_eq!(truncate("中文字符", 2), "中文…[truncated: 2 more chars]");
        assert_eq!(truncate("", 0), "");
        assert_eq!(truncate("a", 0), "…[truncated: 1 more chars]");
    }
}
