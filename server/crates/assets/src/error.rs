//! 这个领域的错误：领域错误（调用方改了输入就能成，交还用户或模型）、写入口拒绝的资源文件、数据库与文件系统出错。

use std::path::PathBuf;

use muliao_platform::errors::DomainError;

use crate::kinds::Section;

/// 讲资源目录怎么改的公共 skill（#501）；写入口的拒绝指向它的小节（[`Section`]，小节标题与它逐字一致）
pub const ASSETS_SKILL: &str = "assets-files";

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Domain(#[from] DomainError),
    #[error(transparent)]
    Rejected(#[from] AssetRejected),
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),
    #[error("{0}")]
    Io(#[from] std::io::Error),
}

impl Error {
    /// 领域错误（含写入口的拒绝，它是 InvalidInput）：交还调用方；别的是系统出错。
    pub fn domain(&self) -> Option<DomainError> {
        match self {
            Error::Domain(e) => Some(e.clone()),
            Error::Rejected(r) => Some(DomainError::invalid(r.reason.clone())),
            Error::Db(_) | Error::Io(_) => None,
        }
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;

pub(crate) fn not_found(message: impl Into<String>) -> Error {
    DomainError::not_found(message).into()
}

pub(crate) fn conflict(message: impl Into<String>) -> Error {
    DomainError::conflict(message).into()
}

pub(crate) fn invalid(message: impl Into<String>) -> Error {
    DomainError::invalid(message).into()
}

pub(crate) fn forbidden(message: impl Into<String>) -> Error {
    DomainError::new(muliao_platform::errors::ErrorKind::Forbidden, message).into()
}

/// 写入口（[`crate::source::AssetSource::write`]）拒绝的一份资源文件（按 InvalidInput 处理）：Display 只是原因（界面上
/// 给用户看）；[`AssetRejected::for_agent`] 是交给 agent 的完整说明（文件工具用）。
#[derive(Debug, Clone, thiserror::Error)]
#[error("{reason}")]
pub struct AssetRejected {
    pub reason: String,
    pub section: Section,
    pub path: PathBuf,
    /// 是谁的资源文件：「the platform's」「agent 'x''s」「no one's」
    pub whose: String,
    pub root: PathBuf,
}

impl AssetRejected {
    pub fn for_agent(&self) -> String {
        format!(
            "{} ({} asset files under {}): {}; nothing was written. The layout and file formats are in the \
             {ASSETS_SKILL} skill, section '{}'.",
            self.path.display(),
            self.whose,
            self.root.display(),
            self.reason,
            self.section.title()
        )
    }
}
