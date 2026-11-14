//! 各领域共用的错误（[`Error`]），只分三类，调用方按类处理：
//! - 领域错误（[`DomainError`]）：调用方（用户或模型）给的输入不对、要的东西不存在、和现状冲突等，改了输入就能成。
//!   HTTP 层按种类映射成 4xx，工具层交还模型修正。
//! - 外部服务（模型接口、转写服务、网页搜索与抓取）出错，按会不会自己好分两种：暂时不可用（[`Error::Unavailable`]，
//!   稍后原样重试可能就成）与出错（[`Error::Upstream`]，重试也不会好）。原因都给用户看（HTTP 502）。
//! - 内部错误：库与文件系统出错、系统自己存的数据坏了、程序错误。HTTP 500（原因只进日志），工具里让这次运行失败。
//!   个别调用方要认出其中某一种（如文件系统出错）时用 [`Error::internal_as`] 按类型认，不加变体。

use std::fmt;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error(transparent)]
    Domain(#[from] DomainError),
    /// 外部服务暂时不可用（网络断、限流、服务端临时故障）
    #[error("{0}")]
    Unavailable(String),
    /// 外部服务出错且不会自己好（拒绝请求、配置不对）
    #[error("{0}")]
    Upstream(String),
    #[error(transparent)]
    Internal(Box<dyn std::error::Error + Send + Sync>),
}

pub type Result<T, E = Error> = std::result::Result<T, E>;

impl Error {
    pub fn internal(e: impl Into<Box<dyn std::error::Error + Send + Sync>>) -> Self {
        Error::Internal(e.into())
    }

    pub fn domain(&self) -> Option<&DomainError> {
        match self {
            Error::Domain(d) => Some(d),
            _ => None,
        }
    }

    /// 内部错误里的某一种（如 `std::io::Error`）
    pub fn internal_as<T: std::error::Error + 'static>(&self) -> Option<&T> {
        match self {
            Error::Internal(e) => e.downcast_ref(),
            _ => None,
        }
    }

    /// 暂时性的失败（外部服务暂时不可用）：稍后原样重试可能就成。
    pub fn is_transient(&self) -> bool {
        matches!(self, Error::Unavailable(_))
    }
}

impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        Error::Internal(Box::new(e))
    }
}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Internal(Box::new(e))
    }
}

/// 存下的数据与代码的约定对不上、或代码自己的不变式被破坏：契约外的失败，让人看见
pub fn broken(message: impl Into<String>) -> Error {
    Error::Internal(message.into().into())
}

pub fn not_found(message: impl Into<String>) -> Error {
    DomainError::not_found(message).into()
}

pub fn conflict(message: impl Into<String>) -> Error {
    DomainError::conflict(message).into()
}

pub fn invalid(message: impl Into<String>) -> Error {
    DomainError::invalid(message).into()
}

pub fn forbidden(message: impl Into<String>) -> Error {
    DomainError::new(ErrorKind::Forbidden, message).into()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    NotFound,
    Conflict,
    InvalidInput,
    /// 身份明确，但无权做这件事
    Forbidden,
    /// 输入超过约定的大小上限
    TooLarge,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DomainError {
    pub kind: ErrorKind,
    pub message: String,
}

impl DomainError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into() }
    }
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::NotFound, message)
    }
    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Conflict, message)
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::InvalidInput, message)
    }
}

impl fmt::Display for DomainError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for DomainError {}
