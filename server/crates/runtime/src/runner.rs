//! 会话运行的端口：组装一次运行（指令、模型、按规则开放的内置工具、连上的 MCP），然后由会话领域一步步驱动
//! （[`Run::step`]：给历史、返回下一步，库不管存库）。每一步里运行端口做三件事：发给模型前把附件展开成模型形态，模型回复后
//! 记一笔用量，工具执行完把交回的图片存进文件存储、把工具挂上的附加数据并进结果——所以会话领域拿到、存下的总是存储形态。
//!
//! 一次运行（[`Run`]）从会话的一次运行开始到结束或挂起：工具组（含 MCP 连接）与这次的 [`RunScope`] 都跟着它，丢掉它就
//! 断开 MCP。压缩（[`Runtime::summarize`]）是另一个入口：无工具的一次文本生成。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use agenter::{
    Agent, Message, Request, RequestPart, Resolution, RunError, RunEvent, Settings, Step, ToolDef, ToolResult,
};
use chrono::Utc;
use chrono_tz::Tz;
use muliao_assets::distribution::Distribution;
use muliao_assets::error::{ASSETS_SKILL, Error as AssetError};
use muliao_assets::memory::MemoryStore;
use muliao_assets::service::AgentService;
use muliao_assets::skills::SkillStore;
use muliao_assets::source::AssetSource;
use muliao_assets::tree::AgentDir;
use muliao_files::{FileStore, FilesError};
use muliao_platform::errors::DomainError;
use muliao_platform::tooling::{Audience, RunScope};
use muliao_usage::{RunKind, UsageEntry, UsageLog, UsageTotals};
use serde_json::Value;

use crate::attachments::{for_model, store_tool_images};
use crate::instructions::{InstructionParts, build_instructions, run_context};
use crate::mcp::{BoxError, McpConnector, skip};
use crate::models::{ModelRegistry, thinking_of};
use crate::toolbox::{Redaction, Toolbox};

/// 组合根交给运行端口的依赖。
pub struct RuntimeDeps {
    pub models: Arc<ModelRegistry>,
    pub toolbox: Arc<Toolbox>,
    pub mcp: Arc<McpConnector>,
    pub source: Arc<AssetSource>,
    pub agents: Arc<AgentService>,
    pub distribution: Arc<Distribution>,
    pub memory: Arc<MemoryStore>,
    pub skills: Arc<SkillStore>,
    pub files: Arc<FileStore>,
    pub usage: Arc<UsageLog>,
    /// 告诉 agent 当前本地时间用的时区
    pub tz: Tz,
}

pub struct Runtime {
    d: RuntimeDeps,
}

/// 开始一次运行要的东西（会话领域给）。
pub struct RunSpec<'a> {
    /// 这次运行用的配置（[`Runtime::config`] 在运行开头取的一份，整次运行与压缩都用它）
    pub config: &'a AgentDir,
    pub session_id: &'a str,
    pub audience: Audience,
    /// 会话来源的说明（会话创建时定下、整个会话不变）
    pub origin_note: &'a str,
    /// 压缩后的早期上下文摘要
    pub summary: Option<&'a str>,
}

#[derive(Debug, thiserror::Error)]
pub enum StartError {
    #[error(transparent)]
    Assets(#[from] AssetError),
    /// agent.toml 里的模型在当前配置下不能运行
    #[error(transparent)]
    Model(#[from] DomainError),
    /// 连 MCP 时系统出错（读不了资源目录、拼不出写沙箱）
    #[error("connecting MCP servers failed: {0}")]
    Mcp(BoxError),
}

#[derive(Debug, thiserror::Error)]
pub enum StepError {
    #[error(transparent)]
    Run(#[from] RunError),
    /// 读附件、存工具交回的图片失败
    #[error(transparent)]
    Files(#[from] FilesError),
}

impl StepError {
    /// 暂时性的失败（模型或工具报的）：稍后从同一份历史原样再走这一步。
    pub fn is_transient(&self) -> bool {
        matches!(self, StepError::Run(e) if e.is_transient())
    }
}

impl Runtime {
    pub fn new(deps: RuntimeDeps) -> Self {
        Self { d: deps }
    }

    pub fn toolbox(&self) -> &Toolbox {
        &self.d.toolbox
    }

    pub fn models(&self) -> &ModelRegistry {
        &self.d.models
    }

    /// agent 现在的配置与资源目录，只认 agent.toml：没加载时 NotFound（带上原因），要它的运行当场失败，不沿用旧值。会话
    /// 的一次运行在开头取一份，整次运行与压缩都用它。
    pub async fn config(&self, agent_id: &str) -> Result<AgentDir, AssetError> {
        Ok(self.d.source.snapshot().await?.loaded_agent(agent_id)?.clone())
    }

    /// 这次运行开头交给模型的运行上下文（当前时间、回答交给谁），由会话领域挂到本次新增的第一条请求上；开始运行时发现的
    /// 问题（[`Run::notices`]）用 [`crate::instructions::with_notices`] 接在后面。
    pub fn run_context(&self, audience: Audience) -> String {
        run_context(Utc::now(), self.d.tz, audience)
    }

    /// 这个 agent 在这个会话里的指令：只由会话级、agent 级的稳定内容组成，同一个会话的两次运行得到同一段文字（缓存前缀）。
    pub async fn instructions(
        &self,
        config: &AgentDir,
        origin_note: &str,
        summary: Option<&str>,
    ) -> Result<String, AssetError> {
        let id = config.id.as_str();
        let memories = self.d.memory.for_run(id).await?;
        let skills = self.d.skills.index(id).await?;
        let workspace = self.d.agents.workspace(id);
        Ok(build_instructions(&InstructionParts {
            agent_id: id,
            agent_name: config.name.as_str(),
            agent_description: config.file.description(),
            workspace: &workspace,
            asset_dir: &config.path,
            origin_note,
            memories: &memories,
            skills: &skills,
            memory_limit: self.d.memory.max_chars(),
            summary,
        }))
    }

    /// 资源目录里与它相关、没加载的文件（读坏、名字冲突），告诉模型去修。
    async fn asset_problems(&self, agent_id: &str) -> Result<Vec<String>, AssetError> {
        let snapshot = self.d.source.snapshot().await?;
        Ok(snapshot
            .problems_for(agent_id)
            .into_iter()
            .map(|p| {
                format!(
                    "Asset file not loaded: {}: {}. Fix the file (or tell an admin if you cannot write it); formats \
                     are in the {ASSETS_SKILL} skill.",
                    p.path.display(),
                    p.reason
                )
            })
            .collect())
    }

    /// 组装一次运行：按 agent 现在的规则开放内置工具，连上它的 MCP server（连不上的跳过，写进 notices），组装指令与模型。
    pub async fn start(&self, spec: RunSpec<'_>) -> Result<Run, StartError> {
        let config = spec.config;
        let agent_id = config.id.as_str();
        let model = self.d.models.resolve(config.file.model())?;
        let workspace = self.d.agents.workspace(agent_id);
        let mut notices = self.asset_problems(agent_id).await?;
        let mut tools = self.d.toolbox.allowed(&self.d.distribution.rules(agent_id).await?);
        let reachable = self.d.mcp.connect_for(agent_id, &workspace).await.map_err(StartError::Mcp)?;
        notices.extend(reachable.notices);
        for connection in reachable.connections {
            let mine = connection.tools::<RunScope>();
            let mut names: Vec<&str> = tools.iter().map(|t| t.def().name.as_str()).collect();
            let clash = mine.iter().map(|t| t.def().name.as_str()).find(|n| {
                let taken = names.contains(n);
                names.push(n);
                taken
            });
            if let Some(name) = clash {
                let reason = format!("MCP server {:?}: its tool name {name:?} is already taken", connection.name());
                notices.push(skip(agent_id, connection.name(), &reason));
                continue;
            }
            tools.extend(mine);
        }
        let instructions = self.instructions(config, spec.origin_note, spec.summary).await?;
        let settings = Settings { thinking: thinking_of(config.file.thinking()), max_tokens: None };
        let agent = Agent::new(model, instructions, settings, tools).expect("tool names were checked to be unique");
        tracing::info!(
            agent = agent_id,
            session_id = spec.session_id,
            model = config.file.model(),
            thinking = config.file.thinking().as_str(),
            tools = agent.tools().len(),
            "agent run started"
        );
        Ok(Run {
            agent,
            scope: RunScope::new(agent_id.to_owned(), workspace, spec.session_id.to_owned(), spec.audience),
            notices,
            redaction: self.d.toolbox.redaction(),
            files: self.d.files.clone(),
            meter: Meter::new(self.d.usage.clone(), agent_id, Some(spec.session_id), config.file.model(), RunKind::Run),
        })
    }

    /// 用这份配置的模型做一次无工具的文本生成（压缩 session_id 这个会话的上下文），用量记为压缩。
    pub async fn summarize(
        &self,
        config: &AgentDir,
        session_id: &str,
        instructions: &str,
        prompt: &str,
    ) -> Result<String, SummarizeError> {
        let model = self.d.models.resolve(config.file.model())?;
        let settings = Settings { thinking: thinking_of(config.file.thinking()), max_tokens: None };
        let agent: Agent<()> = Agent::new(model, instructions, settings, vec![]).expect("no tools");
        let history = [Message::User(Request {
            parts: vec![RequestPart::Text { text: prompt.to_owned() }],
            timestamp: Utc::now(),
            metadata: None,
        })];
        let meter = Meter::new(
            self.d.usage.clone(),
            config.id.as_str(),
            Some(session_id),
            config.file.model(),
            RunKind::Compaction,
        );
        match agent.step(&(), &history, &HashMap::new(), &mut |_| {}).await? {
            Step::Responded(r) => {
                meter.bill(r.usage).await;
                Ok(r.text())
            }
            other => unreachable!("a request without tools ends with a response, got {other:?}"),
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SummarizeError {
    #[error(transparent)]
    Model(#[from] DomainError),
    #[error(transparent)]
    Run(#[from] RunError),
}

/// 一次运行（见模块说明）。
pub struct Run {
    agent: Agent<RunScope>,
    scope: RunScope,
    notices: Vec<String>,
    redaction: Redaction,
    files: Arc<FileStore>,
    meter: Meter,
}

impl Run {
    pub fn scope(&self) -> &RunScope {
        &self.scope
    }

    /// 开始运行时发现、要接在运行上下文后面告诉模型的问题（没加载的资源文件、连不上的 MCP server）。
    pub fn notices(&self) -> &[String] {
        &self.notices
    }

    /// 这次运行交给模型的工具（内置的按注册顺序，再是 MCP 的）。
    pub fn tools(&self) -> &[ToolDef] {
        self.agent.tools()
    }

    /// 走一步（agenter 的 [`Agent::step`]）：history 是存储形态的完整历史，返回的也是存储形态，由调用方保存。
    /// - 末尾是用户一方的消息：附件展开后请求模型，回复后记一笔用量；
    /// - 末尾是带工具调用的回复：执行工具；交回的结果里能内联的图片存进文件存储换成引用，工具挂上的附加数据并进结果。
    ///
    /// on_event 是给界面的实时预览，工具调用的参数已经脱敏（[`Redaction::preview`]）；其中 `ToolFinished` 的结果还是工具
    /// 交回的原样（图片是内容、没有附加数据），保存的以返回值为准（返回的回复没有脱敏，保存前由会话领域脱敏）。
    pub async fn step(
        &self,
        history: &[Message],
        resolved: &HashMap<String, Resolution>,
        on_event: &mut (dyn FnMut(RunEvent<'_>) + Send),
    ) -> Result<Step, StepError> {
        let expanded;
        let sent = if matches!(history.last(), Some(Message::User(_))) {
            expanded = for_model(&self.files, history).await?;
            &expanded[..]
        } else {
            history
        };
        let mut preview = self.redaction.preview(on_event);
        let step = self.agent.step(&self.scope, sent, resolved, &mut preview).await?;
        Ok(match step {
            Step::Responded(r) => {
                self.meter.bill(r.usage).await;
                Step::Responded(r)
            }
            Step::Results(mut req) => {
                for p in &mut req.parts {
                    if let RequestPart::ToolResult(t) = p {
                        self.settle(t).await?;
                    }
                }
                Step::Results(req)
            }
            Step::Paused { mut results, deferred, approvals } => {
                for t in &mut results {
                    self.settle(t).await?;
                }
                Step::Paused { results, deferred, approvals }
            }
            Step::Finished => Step::Finished,
        })
    }

    /// 一步没走完就被停下时，已经做完的工具结果（取自 `ToolFinished` 事件）变成存储形态（同 [`Run::step`] 交回的）。
    pub async fn settled(&self, mut result: ToolResult) -> Result<ToolResult, FilesError> {
        self.settle(&mut result).await?;
        Ok(result)
    }

    /// 工具结果变成存储形态：挂上的附加数据并进 metadata，图片存进文件存储。
    async fn settle(&self, result: &mut ToolResult) -> Result<(), FilesError> {
        if let Some(extra) = self.scope.take_result_metadata(&result.call_id) {
            let mut merged = match result.metadata.take() {
                Some(Value::Object(m)) => m,
                _ => serde_json::Map::new(),
            };
            merged.extend(extra);
            result.metadata = Some(Value::Object(merged));
        }
        store_tool_images(&self.files, result).await
    }
}

impl Drop for Run {
    fn drop(&mut self) {
        let spent = *self.meter.spent.lock().expect("meter lock");
        tracing::info!(
            agent = %self.scope.agent_id,
            session_id = %self.scope.session_id,
            requests = spent.requests,
            input_tokens = spent.input_tokens,
            output_tokens = spent.output_tokens,
            cache_read_tokens = spent.cache_read_tokens,
            cache_write_tokens = spent.cache_write_tokens,
            "agent run finished"
        );
    }
}

/// 一次运行的计量：每次模型回复后记一笔（会话的累计随步骤更新，服务被硬杀只丢在途那次请求的）。用量是账目，不是运行的
/// 一部分：记不上只记一条带数目的 error 日志（便于人工补记），不改变运行的结果。交出去之后调用方被取消的，这笔照样会
/// 入账（UsageLog::record 的约定），按已入账算。
struct Meter {
    usage: Arc<UsageLog>,
    agent_id: String,
    session_id: Option<String>,
    model: String,
    kind: RunKind,
    /// 整次运行花掉的（日志用）
    spent: Mutex<UsageTotals>,
}

impl Meter {
    fn new(usage: Arc<UsageLog>, agent_id: &str, session_id: Option<&str>, model: &str, kind: RunKind) -> Self {
        Self {
            usage,
            agent_id: agent_id.to_owned(),
            session_id: session_id.map(str::to_owned),
            model: model.to_owned(),
            kind,
            spent: Mutex::default(),
        }
    }

    async fn bill(&self, u: agenter::Usage) {
        let totals = UsageTotals {
            requests: 1,
            input_tokens: u.input_tokens,
            output_tokens: u.output_tokens,
            cache_read_tokens: u.cache_read_tokens,
            cache_write_tokens: u.cache_write_tokens,
        };
        *self.spent.lock().expect("meter lock") += totals;
        let entry = UsageEntry {
            agent_id: self.agent_id.clone(),
            session_id: self.session_id.clone(),
            kind: self.kind,
            model: self.model.clone(),
            totals,
        };
        if let Err(e) = self.usage.record(entry).await {
            tracing::error!(agent = %self.agent_id, session_id = ?self.session_id, kind = self.kind.as_str(),
                model = %self.model, input_tokens = totals.input_tokens, output_tokens = totals.output_tokens,
                cache_read_tokens = totals.cache_read_tokens, cache_write_tokens = totals.cache_write_tokens,
                error = %e, "recording model usage failed; this request is not in the usage log");
        }
    }
}
