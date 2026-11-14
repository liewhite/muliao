//! 对话里出现的文件（用户发送的图片与附件、agent 用 share_file 发出的文件、工具交回的图片）：内容不可变，按 id 引用。
//!
//! 做法同 IM 的媒体仓库（Matrix media repository、Slack files）：先上传得到 id，消息里只存引用；文件写入后不再改变、
//! 不删除（删除会话不删文件），聊天记录里的引用永远指向同一份内容。
//!
//! - 元数据（名称、类型、大小、哈希）在 `files` 表，是唯一的索引；内容按 sha256 存在 `<root>/<前两位>/<sha256>`，
//!   相同内容只存一份。
//! - 消息里存的引用是 [`FileRef`]（`muliao-file:<id>/<名称>`），与接口路径无关：改接口路由不影响已存的消息。界面显示
//!   与下载用的地址 [`file_url`]（`/api/files/<id>/<名称>`）由它派生，不存。
//!
//! 用最终正确性换简化：内容与元数据不在一个事务里。先把内容写进临时文件、改名到最终位置，再插入元数据行。
//! - 失败可见：任何一步失败都向上报错，调用方看到的是上传失败；没有返回 id 就没有任何引用。
//! - 可收敛：进程中途被杀留下的临时文件由启动时的 [`FileStore::open`] 清掉；已改名、没有元数据行的内容不会被任何
//!   引用访问到（只能经元数据按 id 找到），同样内容再次上传时直接复用。
//! - 代价：孤儿内容只占空间。

use muliao_platform::ids::{MediaType, new_id};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use chrono::{DateTime, Utc};
use muliao_platform::db::{Database, Migration};
use muliao_platform::errors::{DomainError, Error, ErrorKind, broken};
use percent_encoding::{AsciiSet, NON_ALPHANUMERIC, percent_decode_str, utf8_percent_encode};
use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};

pub const MIGRATION: Migration = Migration { domain: "files", steps: &[include_str!("../migrations/001_files.sql")] };

pub const FILES_PATH: &str = "/api/files";
/// 与界面同源、带着登录 cookie 下载 agent 写的内容时的响应头（对话里的文件、工作目录的文件）：不让浏览器猜类型、在沙箱
/// 里打开。只有能内联显示的图片（[`MediaType::is_inline_image`]）按 inline 返回，其余作为附件（docs/architecture.md 第 15 节）
pub const UNTRUSTED_CONTENT_HEADERS: &[(&str, &str)] = &[
    ("X-Content-Type-Options", "nosniff"),
    ("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"),
];
/// 文件系统（APFS、ext4）对单个文件名的限制是 255 个 UTF-8 字节，名称会原样成为工作目录里的文件名
const NAME_MAX_BYTES: usize = 255;
const CHUNK_BYTES: usize = 64 * 1024;
/// URL 里名称这一段除字母数字和 `-._~` 外都转义（同 Python 的 `quote(name, safe='')`）
const NAME_SEGMENT: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'.').remove(b'_').remove(b'~');

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileMeta {
    pub id: String,
    pub name: String,
    pub media_type: MediaType,
    pub size: u64,
    pub sha256: String,
    pub created_at: DateTime<Utc>,
}

impl FileMeta {
    pub fn url(&self) -> String {
        file_url(&self.id, &self.name)
    }

    pub fn reference(&self) -> FileRef {
        FileRef { id: self.id.clone(), name: self.name.clone() }
    }
}

/// 界面显示与下载文件的地址（接口路径）。
pub fn file_url(file_id: &str, name: &str) -> String {
    format!("{FILES_PATH}/{file_id}/{}", utf8_percent_encode(name, NAME_SEGMENT))
}

/// 消息里对文件存储中一个文件的引用：编号与名字（名字随文件不变，存下来显示时不用再查库）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileRef {
    pub id: String,
    pub name: String,
}

impl FileRef {
    const SCHEME: &str = "muliao-file:";

    /// 存进消息的写法 `muliao-file:<id>/<名称>`（名称按 URL 的一段转义）。
    pub fn uri(&self) -> String {
        format!("{}{}/{}", Self::SCHEME, self.id, utf8_percent_encode(&self.name, NAME_SEGMENT))
    }

    /// [`FileRef::uri`] 的逆。不是这种写法时返回 None（存下的引用对不上，由调用方按数据损坏报）。
    pub fn parse(uri: &str) -> Option<Self> {
        let (id, quoted) = uri.strip_prefix(Self::SCHEME)?.split_once('/')?;
        if id.is_empty() || quoted.is_empty() {
            return None;
        }
        let name = percent_decode_str(quoted).decode_utf8().ok()?;
        Some(FileRef { id: id.to_owned(), name: name.into_owned() })
    }

    /// 界面显示与下载用的地址。
    pub fn url(&self) -> String {
        file_url(&self.id, &self.name)
    }
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

/// 按给出的顺序返回元数据；任何一个不存在都报 NotFound。可以在调用方的事务里用（文件写入后不变、不删，没有先查后写的
/// 竞态）。files 表只经过这里读。
pub fn load_metas(conn: &Connection, file_ids: &[String]) -> Result<Vec<FileMeta>, Error> {
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
    /// 打开文件存储：启动时（还没有上传在进行）调用，先清掉上次进程中途被杀留下的临时文件。
    pub fn open(db: Arc<Database>, root: PathBuf, max_bytes: u64) -> io::Result<Self> {
        let store = Self { db, root, max_bytes };
        store.recover()?;
        Ok(store)
    }

    pub fn max_bytes(&self) -> u64 {
        self.max_bytes
    }

    fn tmp_dir(&self) -> PathBuf {
        self.root.join("tmp")
    }

    pub fn blob_path(&self, meta: &FileMeta) -> PathBuf {
        self.blob_of(&meta.sha256)
    }

    /// 内容按 sha256 存放的位置
    fn blob_of(&self, sha256: &str) -> PathBuf {
        self.root.join(&sha256[..2]).join(sha256)
    }

    fn recover(&self) -> io::Result<()> {
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
        media_type: MediaType,
        mut body: impl AsyncRead + Unpin,
    ) -> Result<FileMeta, Error> {
        let name = check_name(name)?.to_string();
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
        let blob = self.blob_of(&sha256);
        let from = tmp.0.clone();
        tokio::task::spawn_blocking(move || commit_blob(&from, &blob))
            .await
            .unwrap_or_else(|e| std::panic::resume_unwind(e.into_panic()))?;
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

    pub async fn put_path(&self, name: &str, media_type: MediaType, path: &Path) -> Result<FileMeta, Error> {
        let file = tokio::fs::File::open(path).await?;
        self.put(name, media_type, file).await
    }

    pub async fn get(&self, file_id: &str) -> Result<FileMeta, Error> {
        let mut metas = self.get_many(vec![file_id.to_string()]).await?;
        Ok(metas.remove(0))
    }

    /// 调用方给出的 id（可能有误）：不存在报 NotFound。
    pub async fn get_many(&self, file_ids: Vec<String>) -> Result<Vec<FileMeta>, Error> {
        self.db.read(move |c| load_metas(c, &file_ids)).await
    }

    /// 系统自己存下的引用（消息里的附件）：找不到是数据损坏（内部错误），不是输入错误。
    pub async fn referenced(&self, file_ids: Vec<String>) -> Result<Vec<FileMeta>, Error> {
        let (metas, missing) = self.db.read(move |c| lookup(c, &file_ids)).await?;
        if !missing.is_empty() {
            return Err(broken(format!("stored references point to missing files: {missing:?}")));
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
