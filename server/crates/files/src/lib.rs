//! 对话里出现的文件（用户发送的图片与附件、agent 用 share_file 发出的文件、工具交回的图片）：内容不可变，按 id 引用。
//!
//! 做法同 IM 的媒体仓库（Matrix media repository、Slack files）：先上传得到 id，消息里只存引用；文件写入后不再改变、
//! 不删除（删除会话不删文件），聊天记录里的引用永远指向同一份内容。
//!
//! - 元数据（名称、类型、大小、哈希）在 `files` 表，是唯一的索引；内容按 sha256 存在 `<root>/<前两位>/<sha256>`，
//!   相同内容只存一份。
//! - 引用的唯一形式是 [`file_url`]：`/api/files/<id>/<名称>`，界面直接用它显示与下载，消息里的附件也存它。
//!
//! 用最终正确性换简化：内容与元数据不在一个事务里。先把内容写进临时文件、改名到最终位置，再插入元数据行。
//! - 失败可见：任何一步失败都向上报错，调用方看到的是上传失败；没有返回 id 就没有任何引用。
//! - 可收敛：进程中途被杀留下的临时文件由启动时的 [`FileStore::recover`] 清掉；已改名、没有元数据行的内容不会被任何
//!   引用访问到（只能经元数据按 id 找到），同样内容再次上传时直接复用。
//! - 代价：孤儿内容只占空间。

use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, Utc};
use muliao_platform::db::{Database, Migration};
use muliao_platform::errors::{DomainError, ErrorKind};
use muliao_platform::ids::new_id;
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, percent_decode_str, utf8_percent_encode};
use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};

pub const MIGRATION: Migration = Migration { domain: "files", steps: &[include_str!("../migrations/001_files.sql")] };

pub const FILES_PATH: &str = "/api/files";
/// 浏览器可以直接显示、模型也普遍能看的图片类型：只有这些按 inline 返回，并作为图片交给模型。SVG 不在其中：它能带脚本
pub const INLINE_IMAGE_TYPES: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];
/// 与界面同源、带着登录 cookie 下载 agent 写的内容时的响应头（对话里的文件、工作目录的文件）：不让浏览器猜类型、在沙箱
/// 里打开。只有 [`INLINE_IMAGE_TYPES`] 按 inline 返回，其余作为附件（docs/architecture.md 第 15 节）
pub const UNTRUSTED_CONTENT_HEADERS: &[(&str, &str)] = &[
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"),
];
/// 文件系统（APFS、ext4）对单个文件名的限制是 255 个 UTF-8 字节，名称会原样成为工作目录里的文件名
const NAME_MAX_BYTES: usize = 255;
const CHUNK_BYTES: usize = 64 * 1024;
/// URL 里名称这一段除字母数字和 `-._~` 外都转义（同 Python 的 `quote(name, safe='')`）
const NAME_SEGMENT: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'.').remove(b'_').remove(b'~');

#[derive(Debug, thiserror::Error)]
pub enum FilesError {
    /// 调用方给的输入不对（名称、类型、超过大小上限、id 不存在）
    #[error(transparent)]
    Domain(#[from] DomainError),
    /// 系统自己存下的引用（消息里的附件）指向不存在的文件：文件从不删除，这是不变量被破坏，不是输入错误
    #[error("stored references point to missing files: {0:?}")]
    Corrupt(Vec<String>),
    #[error("file storage: {0}")]
    Io(#[from] io::Error),
    #[error(transparent)]
    Db(#[from] rusqlite::Error),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileMeta {
    pub id: String,
    pub name: String,
    pub media_type: String,
    pub size: u64,
    pub sha256: String,
    pub created_at: DateTime<Utc>,
}

impl FileMeta {
    pub fn url(&self) -> String {
        file_url(&self.id, &self.name)
    }

    pub fn is_inline_image(&self) -> bool {
        INLINE_IMAGE_TYPES.contains(&self.media_type.as_str())
    }
}

pub fn file_url(file_id: &str, name: &str) -> String {
    format!("{FILES_PATH}/{file_id}/{}", utf8_percent_encode(name, NAME_SEGMENT))
}

/// [`file_url`] 的逆：取出 (id, 名称)。不是 file_url 生成的地址时返回 None（存下的引用对不上，由调用方按数据损坏报）。
pub fn parse_file_url(url: &str) -> Option<(String, String)> {
    let (file_id, quoted) = url.strip_prefix(FILES_PATH)?.strip_prefix('/')?.split_once('/')?;
    if file_id.is_empty() || quoted.is_empty() {
        return None;
    }
    let name = percent_decode_str(quoted).decode_utf8().ok()?;
    Some((file_id.to_string(), name.into_owned()))
}

/// 文件名会成为 URL 的一段和 agent 工作目录里的文件名：只接受单个路径段，不做修正。
pub fn check_name(name: &str) -> Result<&str, DomainError> {
    if name.is_empty() || name.len() > NAME_MAX_BYTES {
        return Err(DomainError::invalid(format!(
            "file name must be 1..{NAME_MAX_BYTES} bytes in UTF-8, got {}",
            name.len()
        )));
    }
    if name == "." || name == ".." || name.chars().any(|c| c == '/' || c == '\\' || c.is_control()) {
        return Err(DomainError::invalid(format!(
            "file name {name:?} must be a single path segment without control characters"
        )));
    }
    Ok(name)
}

/// 只存 type/subtype（小写）：参数（如 charset）不影响怎么显示和交给模型，下载一律按原始字节。
pub fn check_media_type(media_type: &str) -> Result<String, DomainError> {
    let essence = media_type.split(';').next().unwrap_or_default().trim();
    let valid = match essence.split_once('/') {
        Some((kind, subtype)) => !kind.is_empty() && !subtype.is_empty() && !essence.chars().any(char::is_whitespace),
        None => false,
    };
    if !valid {
        return Err(DomainError::invalid(format!("media type {media_type:?} must be 'type/subtype'")));
    }
    Ok(essence.to_lowercase())
}

/// 按给出的顺序返回元数据；任何一个不存在都报 NotFound。可以在调用方的事务里用（文件写入后不变、不删，没有先查后写的
/// 竞态）。files 表只经过这里读。
pub fn load_metas(conn: &Connection, file_ids: &[String]) -> Result<Vec<FileMeta>, FilesError> {
    let (metas, missing) = lookup(conn, file_ids)?;
    if !missing.is_empty() {
        return Err(DomainError::not_found(format!("files {missing:?} not found")).into());
    }
    Ok(metas)
}

/// 找到的元数据（按给出的顺序）与找不到的 id。
fn lookup(conn: &Connection, file_ids: &[String]) -> rusqlite::Result<(Vec<FileMeta>, Vec<String>)> {
    let mut stmt =
        conn.prepare_cached("SELECT id, name, media_type, size, sha256, created_at FROM files WHERE id = ?1")?;
    let mut metas = Vec::with_capacity(file_ids.len());
    let mut missing = Vec::new();
    for id in file_ids {
        let row = stmt
            .query_row([id], |r| {
                Ok(FileMeta {
                    id: r.get(0)?,
                    name: r.get(1)?,
                    media_type: r.get(2)?,
                    size: r.get(3)?,
                    sha256: r.get(4)?,
                    created_at: r.get(5)?,
                })
            })
            .optional()?;
        match row {
            Some(m) => metas.push(m),
            None => missing.push(id.clone()),
        }
    }
    Ok((metas, missing))
}

/// 正在写的临时文件：丢掉时删除（写完改名后它已不在，删除失败不管）。上传中途出错或调用方被取消都不留下临时文件。
struct TmpFile(PathBuf);

impl Drop for TmpFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

pub struct FileStore {
    db: Arc<Database>,
    root: PathBuf,
    max_bytes: u64,
}

impl FileStore {
    pub fn new(db: Arc<Database>, root: PathBuf, max_bytes: u64) -> Self {
        Self { db, root, max_bytes }
    }

    pub fn max_bytes(&self) -> u64 {
        self.max_bytes
    }

    fn tmp_dir(&self) -> PathBuf {
        self.root.join("tmp")
    }

    pub fn blob_path(&self, meta: &FileMeta) -> PathBuf {
        self.root.join(&meta.sha256[..2]).join(&meta.sha256)
    }

    /// 启动时（还没有上传在进行）清掉上次进程中途被杀留下的临时文件。
    pub fn recover(&self) -> io::Result<()> {
        let entries = match std::fs::read_dir(self.tmp_dir()) {
            Ok(e) => e,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e),
        };
        let mut count = 0;
        for entry in entries {
            std::fs::remove_file(entry?.path())?;
            count += 1;
        }
        if count > 0 {
            tracing::warn!(count, "removed unfinished uploads");
        }
        Ok(())
    }

    /// 存一个文件：边读边写边算哈希，超过上限立即停止读取（TooLarge）。
    pub async fn put(
        &self,
        name: &str,
        media_type: &str,
        mut body: impl AsyncRead + Unpin,
    ) -> Result<FileMeta, FilesError> {
        let name = check_name(name)?.to_string();
        let media_type = check_media_type(media_type)?;
        let tmp_dir = self.tmp_dir();
        tokio::fs::create_dir_all(&tmp_dir).await?;
        let tmp = TmpFile(tmp_dir.join(new_id()));
        let mut file = tokio::fs::File::create(&tmp.0).await?;
        let mut digest = Sha256::new();
        let mut size: u64 = 0;
        let mut buf = vec![0u8; CHUNK_BYTES];
        loop {
            let n = body.read(&mut buf).await?;
            if n == 0 {
                break;
            }
            size += n as u64;
            if size > self.max_bytes {
                return Err(
                    DomainError::new(ErrorKind::TooLarge, format!("file exceeds {} bytes", self.max_bytes)).into()
                );
            }
            digest.update(&buf[..n]);
            file.write_all(&buf[..n]).await?;
        }
        file.sync_all().await?;
        drop(file);
        let sha256 = format!("{:x}", digest.finalize());
        let blob = self.root.join(&sha256[..2]).join(&sha256);
        let from = tmp.0.clone();
        tokio::task::spawn_blocking(move || commit_blob(&from, &blob))
            .await
            .unwrap_or_else(|e| std::panic::resume_unwind(e.into_panic()))?;
        drop(tmp);
        let meta = FileMeta { id: new_id(), name, media_type, size, sha256, created_at: Utc::now() };
        let row = meta.clone();
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO files (id, sha256, name, media_type, size, created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    rusqlite::params![row.id, row.sha256, row.name, row.media_type, row.size, row.created_at],
                )
            })
            .await?;
        tracing::info!(file_id = %meta.id, media_type = %meta.media_type, size, "file stored");
        Ok(meta)
    }

    pub async fn put_path(&self, name: &str, media_type: &str, path: &Path) -> Result<FileMeta, FilesError> {
        let file = tokio::fs::File::open(path).await?;
        self.put(name, media_type, file).await
    }

    pub async fn get(&self, file_id: &str) -> Result<FileMeta, FilesError> {
        let mut metas = self.get_many(vec![file_id.to_string()]).await?;
        Ok(metas.remove(0))
    }

    /// 调用方给出的 id（可能有误）：不存在报 NotFound。
    pub async fn get_many(&self, file_ids: Vec<String>) -> Result<Vec<FileMeta>, FilesError> {
        self.db.read(move |c| load_metas(c, &file_ids)).await
    }

    /// 系统自己存下的引用（消息里的附件）：找不到是数据损坏（[`FilesError::Corrupt`]），不是输入错误。
    pub async fn referenced(&self, file_ids: Vec<String>) -> Result<Vec<FileMeta>, FilesError> {
        let (metas, missing) = self.db.read(move |c| lookup(c, &file_ids)).await?;
        if !missing.is_empty() {
            return Err(FilesError::Corrupt(missing));
        }
        Ok(metas)
    }

    pub async fn read(&self, meta: &FileMeta) -> io::Result<Vec<u8>> {
        tokio::fs::read(self.blob_path(meta)).await
    }
}

/// 已有的内容不覆盖（它可能已被引用，原件不可变）；新内容（已落盘）改名到位，再让目录项落盘：掉电后不会出现元数据行
/// 指向空文件或截断内容的情况。
fn commit_blob(tmp: &Path, blob: &Path) -> io::Result<()> {
    if blob.exists() {
        return Ok(());
    }
    let dir = blob.parent().expect("blob path has a parent");
    std::fs::create_dir_all(dir)?;
    std::fs::rename(tmp, blob)?;
    std::fs::File::open(dir)?.sync_all()
}

#[cfg(test)]
mod tests;
