//! 工作区领域的错误：领域错误（调用方改了输入就能成：交还用户或模型），其余是系统出错。

use agenter::ToolError;
use muliao_platform::errors::DomainError;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Domain(#[from] DomainError),
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),
    #[error(transparent)]
    Assets(muliao_assets::error::Error),
    #[error(transparent)]
    Sessions(muliao_sessions::error::Error),
    #[error("{0}")]
    Io(#[from] std::io::Error),
    /// 存下的数据与代码的约定对不上：契约外的失败，让人看见
    #[error("stored workspace data is inconsistent: {0}")]
    Broken(String),
}

impl From<muliao_assets::error::Error> for Error {
    fn from(e: muliao_assets::error::Error) -> Self {
        match e.domain() {
            Some(d) => Error::Domain(d),
            None => Error::Assets(e),
        }
    }
}

impl From<muliao_sessions::error::Error> for Error {
    fn from(e: muliao_sessions::error::Error) -> Self {
        match e {
            muliao_sessions::error::Error::Domain(d) => Error::Domain(d),
            other => Error::Sessions(other),
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

/// 工具层的统一规则：领域错误交还模型，其余让这次运行失败。
impl From<Error> for ToolError {
    fn from(e: Error) -> Self {
        match e {
            Error::Domain(d) => d.into(),
            other => ToolError::Abort(Box::new(other)),
        }
    }
}

pub type Result<T, E = Error> = std::result::Result<T, E>;

pub fn not_found(message: impl Into<String>) -> Error {
    DomainError::not_found(message).into()
}

pub fn conflict(message: impl Into<String>) -> Error {
    DomainError::conflict(message).into()
}

pub fn invalid(message: impl Into<String>) -> Error {
    DomainError::invalid(message).into()
}
