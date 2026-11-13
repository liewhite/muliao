//! 会话领域的端到端与库结构升级测试：放在 crate 里，用模块内部的读写函数，不为测试把它们开放给别的领域。

mod actor;
mod holds;
mod migrations;
