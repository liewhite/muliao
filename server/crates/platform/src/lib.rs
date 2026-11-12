//! 平台层：各领域共用的机制，不认识任何领域的词汇（会话、issue、agent 这些词都不出现在这里）。
//!
//! - [`config`]：读 `~/.muliao/config.toml`，所有运行参数的唯一来源。
//! - [`db`]：SQLite 单写者事务、只读连接、按领域分的迁移。
//! - [`fs`]：路径的身份（APFS 不分大小写与正规化形式）、原子写。
//! - [`vault`]：secret 的加解密（Fernet，与 Python 版同一种密文）。
//! - [`sandbox`] 与 [`shell`]：起 bash 命令的底层（进程组、secret 注入与输出脱敏、写沙箱）。
//!   以哪个 agent 的身份起（注入哪些 secret、能写哪些路径）由工作区领域拼好交进来。
//! - [`bus`]：推给前端 SSE 的展示事件（只做展示加速，库是唯一事实源）。
//! - [`tooling`] 与 [`tool_views`]：工具的共同契约（运行上下文、内置工具的声明、错误交还模型的规则）与界面展示块。
//! - [`ids`]、[`errors`]、[`logging`]、[`text`]。

pub mod bus;
pub mod config;
pub mod db;
pub mod errors;
pub mod fs;
pub mod ids;
pub mod logging;
pub mod sandbox;
pub mod shell;
pub mod text;
pub mod tool_views;
pub mod tooling;
pub mod vault;
