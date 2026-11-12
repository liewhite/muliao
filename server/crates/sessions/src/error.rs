//! 会话领域的错误：领域错误（调用方改了输入就能成：交还用户或模型）、存下的数据与代码对不上、数据库与别的领域出错。

use muliao_files::FilesError;
use muliao_platform::errors::{DomainError, ErrorKind};

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Domain(#[from] DomainError),
    /// 存下的数据与代码的约定对不上（消息解析不了、历史破了不变式）：契约外的失败，不重试，让人看见
    #[error("stored session data is inconsistent: {0}")]
    Broken(String),
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),
    #[error(transparent)]
    Assets(muliao_assets::error::Error),
    #[error(transparent)]
    Files(FilesError),
    /// 会话事件的订阅者（别的领域）出错：发布方的事务整个回滚
    #[error("a session event subscriber failed: {0}")]
    Subscriber(crate::events::BoxError),
}

impl From<FilesError> for Error {
    fn from(e: FilesError) -> Self {
        match e {
            FilesError::Domain(d) => Error::Domain(d),
            other => Error::Files(other),
        }
    }
}

impl From<muliao_assets::error::Error> for Error {
    fn from(e: muliao_assets::error::Error) -> Self {
        match e.domain() {
            Some(d) => Error::Domain(d),
            None => Error::Assets(e),
        }
    }
}

impl Error {
    pub fn domain(&self) -> Option<&DomainError> {
        match self {
            Error::Domain(e) => Some(e),
            _ => None,
        }
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;

pub(crate) fn broken(message: impl Into<String>) -> Error {
    Error::Broken(message.into())
}

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
    DomainError::new(ErrorKind::Forbidden, message).into()
}

/// 工具里的会话领域错误：领域错误交还模型修正，其余让这次运行失败（muliao_platform::tooling 的统一规则）。
impl From<Error> for agenter::ToolError {
    fn from(e: Error) -> Self {
        match e {
            Error::Domain(d) => d.into(),
            other => agenter::ToolError::Abort(Box::new(other)),
        }
    }
}
