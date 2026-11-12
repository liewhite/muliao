//! 资源目录与 agent：`~/.muliao/assets` 的快照与写入口、分发规则与角色（access.toml）、agent.toml、agents 表与生命周期
//! （建、改名、归档、默认、删除），以及各种资源的读写（记忆、skill、MCP server、secret、资源池）。设计见
//! docs/architecture.md 第 1 节「资源目录」、第 6 节「工具与分发」「角色」、第 10 节、第 13 节。
//!
//! - [`source::AssetSource`]：当前快照（[`tree::Snapshot`]）、改写资源文件的写入口、平台自己写时的锁、文件变动监听。
//! - [`tree`]：布局、读树、快照；[`formats`]：每种文件的格式；[`kinds`]：种类的唯一定义；[`asset_files`]：写前校验。
//! - [`rules`]：`action:kind:glob` 规则、角色、内置工具表；[`access_file`]：access.toml 的读写；[`access`]：能用 / 能改的
//!   唯一判断及派生（reach、分发情况、写沙箱的领域输入）；[`distribution`]：角色与 agent 规则的门面。
//! - [`agents`]：agents 表（别的领域在自己的事务里用 `*_in` 查询）；[`service`]：建、改、默认、查看；[`lifecycle`]：删除与
//!   归档（各领域的清理与归档前检查由组合根登记）。
//! - [`memory`]、[`skills`]、[`mcp`]、[`secrets`]、[`catalog`]：各种资源的读写。
//!
//! - [`tools`]：本领域的工具（list_agents、get_agent、create_agent、delete_agent、vault_set）。

pub mod access;
pub mod access_file;
pub mod agents;
pub mod asset_files;
pub mod catalog;
pub mod distribution;
pub mod error;
pub mod formats;
pub mod kinds;
pub mod lifecycle;
pub mod mcp;
pub mod memory;
pub mod naming;
pub mod rules;
pub mod secrets;
pub mod service;
pub mod skills;
pub mod source;
pub mod tools;
pub mod tree;

pub use error::{AssetRejected, Error, Result};
