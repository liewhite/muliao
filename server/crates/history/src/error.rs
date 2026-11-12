//! 历史领域的错误：领域错误（调用方改了输入就能成：交还用户或模型）、存下的数据与代码对不上、数据库与别的领域出错。

use muliao_platform::errors::DomainError;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Domain(#[from] DomainError),
    /// 存下的历史与代码的约定对不上：契约外的失败，让人看见
    #[error("stored history data is inconsistent: {0}")]
    Broken(String),
    #[error("database error: {0}")]
    Db(#[from] rusqlite::Error),
    #[error(transparent)]
    Sessions(muliao_sessions::error::Error),
    #[error(transparent)]
    Issues(muliao_issues::Error),
    #[error(transparent)]
    Assets(muliao_assets::error::Error),
}

impl From<muliao_sessions::error::Error> for Error {
    fn from(e: muliao_sessions::error::Error) -> Self {
        match e {
            muliao_sessions::error::Error::Domain(d) => Error::Domain(d),
            other => Error::Sessions(other),
        }
    }
}

impl From<muliao_issues::Error> for Error {
    fn from(e: muliao_issues::Error) -> Self {
        match e {
            muliao_issues::Error::Domain(d) => Error::Domain(d),
            other => Error::Issues(other),
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

pub(crate) fn invalid(message: impl Into<String>) -> Error {
    DomainError::invalid(message).into()
}

/// 工具里的历史领域错误：领域错误交还模型修正，其余让这次运行失败（muliao_platform::tooling 的统一规则）。
impl From<Error> for agenter::ToolError {
    fn from(e: Error) -> Self {
        match e {
            Error::Domain(d) => d.into(),
            other => agenter::ToolError::Abort(Box::new(other)),
        }
    }
}
