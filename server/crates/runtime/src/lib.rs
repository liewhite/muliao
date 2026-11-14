//! 会话运行的端口（docs/architecture.md 2.1）：模型、指令组装、工具组合、MCP 连接、附件在存储形态与模型形态之间的转换，
//! 以及逐步驱动 agenter 的接口。不是业务领域，也不存库（用量账目除外：调用模型的只有这里）：每一步的产物交给会话领域保存。
//!
//! - [`models`]：按配置构造模型，每个 provider 一个并发上限，列模型。
//! - [`instructions`]：指令（缓存前缀，只放运行间不变的）与运行上下文（随运行变化的）。
//! - [`toolbox`]：各领域带着声明交来的内置工具（声明的类型与错误交还模型的规则在 `muliao_platform::tooling`），收集成分发
//!   用的工具表、界面展示规则、脱敏，按 agent 的规则开放。
//! - [`mcp`]：按配置与 secret 连 MCP server，关进使用者的写沙箱，失败只影响它自己并告诉模型。
//! - [`attachments`]、[`images`]、[`conventions`]：附件与工具交回的图片。
//! - [`runner`]：组装一次运行（[`runner::Runtime::start`]）与逐步驱动（[`runner::Run::respond`] 请求模型、[`runner::Run::run_tools`] 执行工具），压缩用的文本生成。

pub mod attachments;
pub mod conventions;
pub mod images;
pub mod instructions;
pub mod mcp;
pub mod models;
pub mod runner;
pub mod toolbox;

pub use runner::{Run, RunSpec, Runtime, RuntimeDeps, StepError, ToolsRun};
