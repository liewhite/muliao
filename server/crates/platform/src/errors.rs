//! 领域错误：调用方（用户或模型）给的输入不对、要的东西不存在、和现状冲突等，调用方改了输入就能成。
//! HTTP 层按种类映射成 4xx（[`ErrorKind::ScriptFailed`] 为 502），工具层原样交还模型修正。
//! 程序错误、系统自己存的数据坏了、外部服务出错不是领域错误，各自用别的错误类型报出。

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    NotFound,
    Conflict,
    InvalidInput,
    /// 调用方证明不了自己的身份
    Unauthorized,
    /// 身份明确，但无权做这件事
    Forbidden,
    /// 输入超过约定的大小上限
    TooLarge,
    /// 系统替调用方执行它写的脚本（如触发器的前置检查）失败：非 0 退出、超时、输出过大
    ScriptFailed,
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
