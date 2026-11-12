//! 托管进程的输出（纯 IO，不碰数据库）：把输出逐块脱敏写进日志、读日志的最后几行。后台任务与流式触发器共用。

use std::collections::BTreeMap;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use muliao_platform::shell::{Output, redact};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::text::split_lines;

const CHUNK: usize = 65536;
const TAIL_LINES: usize = 30;
const TAIL_BYTES: u64 = 16384;

/// 日志的最后几行（读文件末尾一段，不整个读进来）。没有这个文件是空串。
pub fn tail(path: &Path) -> std::io::Result<String> {
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(String::new()),
        Err(e) => return Err(e),
    };
    let size = f.seek(SeekFrom::End(0))?;
    f.seek(SeekFrom::Start(size.saturating_sub(TAIL_BYTES)))?;
    let mut data = Vec::new();
    f.read_to_end(&mut data)?;
    let text = String::from_utf8_lossy(&data);
    let mut lines = split_lines(&text);
    if size > TAIL_BYTES && !lines.is_empty() {
        // 第一行可能被截在中间
        lines.remove(0);
    }
    let from = lines.len().saturating_sub(TAIL_LINES);
    Ok(lines[from..].join("\n"))
}

fn find_from(hay: &str, needle: &str, from: usize) -> Option<usize> {
    let (h, n) = (hay.as_bytes(), needle.as_bytes());
    if from > h.len() || n.is_empty() {
        return None;
    }
    h[from..].windows(n.len()).position(|w| w == n).map(|i| i + from)
}

/// 把切口挪到任何 secret 出现位置之前：切口两侧分开脱敏时，跨在切口上的 secret 会漏出明文。挪一次可能让别的 secret（或
/// 同一个 secret 的另一次出现）跨到新切口上，所以挪到不再变化为止。
fn safe_cut(text: &str, mut cut: usize, secrets: &BTreeMap<String, String>) -> usize {
    loop {
        let mut moved = cut;
        for value in secrets.values().filter(|v| !v.is_empty()) {
            let mut start = find_from(text, value, (moved + 1).saturating_sub(value.len()));
            while let Some(s) = start.filter(|s| *s < moved) {
                if s + value.len() > moved {
                    moved = s;
                }
                start = find_from(text, value, s + 1);
            }
        }
        if moved == cut {
            return cut;
        }
        cut = moved;
    }
}

/// text 末尾最长的、恰好是某个 secret 开头（真前缀）的一段的长度：这一段可能和下一块拼成 secret，要留着。
fn partial_secret(text: &str, secrets: &BTreeMap<String, String>) -> usize {
    let mut longest = 0;
    for value in secrets.values() {
        let top = value.len().saturating_sub(1).min(text.len());
        for n in (longest + 1..=top).rev() {
            if value.is_char_boundary(n) && text.ends_with(&value[..n]) {
                longest = n;
                break;
            }
        }
    }
    longest
}

/// 增量 UTF-8 解码：解出 buf 里完整的部分（坏字节换成 U+FFFD），末尾不完整的字符留在 buf 里等下一块；final 时全部解出。
fn decode(buf: &mut Vec<u8>, last: bool) -> String {
    let mut out = String::new();
    let mut at = 0;
    loop {
        match std::str::from_utf8(&buf[at..]) {
            Ok(s) => {
                out.push_str(s);
                at = buf.len();
                break;
            }
            Err(e) => {
                let valid = at + e.valid_up_to();
                out.push_str(std::str::from_utf8(&buf[at..valid]).expect("valid prefix"));
                match e.error_len() {
                    Some(bad) => {
                        out.push('\u{FFFD}');
                        at = valid + bad;
                    }
                    None if last => {
                        out.push('\u{FFFD}');
                        at = buf.len();
                        break;
                    }
                    None => {
                        at = valid;
                        break;
                    }
                }
            }
        }
    }
    buf.drain(..at);
    out
}

/// 把输出逐块脱敏后追加到日志。每读一块写出到切口为止：末尾可能是某个 secret 开头的一段留着等下一块（[`partial_secret`]），
/// 切口再避开跨在它上面的完整 secret（[`safe_cut`]），两侧分开脱敏也不会漏。secret 可以含换行，所以不能按行切。读到结尾时
/// 写出全部剩余；读到一半被取消（进程退出后后代还占着管道）时，只丢下留着的那一小段。增量解码，多字节字符不会被块边界
/// 切坏。
pub async fn record_output(
    stream: &mut Output,
    log_file: &Path,
    secrets: &BTreeMap<String, String>,
) -> std::io::Result<()> {
    let mut out = tokio::fs::OpenOptions::new().create(true).append(true).open(log_file).await?;
    let mut raw = Vec::new();
    let mut pending = String::new();
    let mut chunk = vec![0u8; CHUNK];
    loop {
        let n = stream.read(&mut chunk).await?;
        if n == 0 {
            break;
        }
        raw.extend_from_slice(&chunk[..n]);
        pending.push_str(&decode(&mut raw, false));
        let cut = safe_cut(&pending, pending.len() - partial_secret(&pending, secrets), secrets);
        out.write_all(redact(&pending[..cut], secrets).as_bytes()).await?;
        pending.drain(..cut);
        out.flush().await?;
    }
    pending.push_str(&decode(&mut raw, true));
    out.write_all(redact(&pending, secrets).as_bytes()).await?;
    out.flush().await
}

#[cfg(test)]
mod tests {
    use std::pin::Pin;

    use super::*;

    fn secrets() -> BTreeMap<String, String> {
        BTreeMap::from([("K".to_owned(), "s3cr€t".to_owned()), ("L".to_owned(), "line\nbreak".to_owned())])
    }

    async fn recorded(chunks: Vec<Vec<u8>>) -> String {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("x.log");
        let (mut tx, rx) = tokio::io::duplex(4);
        let writer = tokio::spawn(async move {
            for c in chunks {
                tx.write_all(&c).await.unwrap();
                tx.flush().await.unwrap();
                tokio::task::yield_now().await;
            }
        });
        let mut stream: Output = Pin::from(Box::new(rx) as Box<dyn tokio::io::AsyncRead + Send>);
        record_output(&mut stream, &log, &secrets()).await.unwrap();
        writer.await.unwrap();
        std::fs::read_to_string(&log).unwrap()
    }

    #[tokio::test]
    async fn secrets_split_across_chunks_are_still_redacted() {
        let text = "a s3cr€t b line\nbreak c s3cr";
        let bytes = text.as_bytes();
        // 每个字节一块：secret 与多字节字符都被切开
        let out = recorded(bytes.iter().map(|b| vec![*b]).collect()).await;
        assert_eq!(out, "a [secret:K] b [secret:L] c s3cr");
    }

    #[tokio::test]
    async fn bad_bytes_become_replacement_characters() {
        let out = recorded(vec![b"ok \xff\xfe".to_vec(), "é".as_bytes()[..1].to_vec()]).await;
        assert_eq!(out, "ok \u{FFFD}\u{FFFD}\u{FFFD}");
    }

    #[test]
    fn tail_keeps_the_last_whole_lines() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("x.log");
        assert_eq!(tail(&log).unwrap(), "");
        let text: String = (0..5000).map(|i| format!("line {i}\n")).collect();
        std::fs::write(&log, text).unwrap();
        let t = tail(&log).unwrap();
        assert_eq!(t.lines().count(), 30);
        assert!(t.ends_with("line 4999") && t.starts_with("line 4970"));
    }
}
