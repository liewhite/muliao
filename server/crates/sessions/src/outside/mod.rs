//! 外部工作：在会话之外替它做的事（后台任务、到点唤醒、委派）的占用、交回与结清。

pub(crate) mod delegation;
pub(crate) mod holds;
pub(crate) mod settlement;
pub(crate) mod wakeups;
pub mod work;
