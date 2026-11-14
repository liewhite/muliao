//! 挂起：停下来等回信的调用（问用户、问 agent、等批准）、它们的回答与会话停在哪。

pub(crate) mod answers;
pub(crate) mod paused_calls;
pub(crate) mod pending;
