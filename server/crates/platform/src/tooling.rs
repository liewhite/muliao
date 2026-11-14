//! 工具的共同契约（各领域的工具都依赖它，包括运行端口之下的资源目录与 web，所以它在平台层）：
//!
//! - [`RunScope`]：一次运行交给每个工具的上下文（agenter 的 deps），[`Audience`]：这次运行的回答交给谁。
//! - [`ToolTraits`]：内置工具自己声明的属性（读写、管理类、要脱敏的参数、界面展示、是否排队执行）。内置工具只能经
//!   [`builtin`] 带着声明交出（[`BuiltinTool`]），没声明的写不出来；运行端口的工具箱收集后交给分发、脱敏和界面。
//! - 错误交给模型的统一规则（见 [`Error`] 到 `ToolError` 的转换）。
//!
//! **错误映射的统一规则**（各领域的工具都照这个，不各写一份）：
//! - 调用方改了输入就能成的（[`DomainError`]：参数不对、找不到、冲突……）→ `ToolError::Retry`：交还模型修正，连续次数有
//!   上限（agenter 的 `MAX_RETRIES`），超了按失败交回并提示换做法（同 Python 的 DomainError → ModelRetry）。
//! - 外部服务暂时不可用（连不上、超时、429、5xx）→ `ToolError::Unavailable`：整次运行按暂时性失败，稍后重试。
//! - 其余（配置或账户问题、程序错误、系统自己的数据坏了）→ `ToolError::Abort`：整次运行失败，让人看见。

use crate::ids::{AgentId, SessionId};
use std::any::{Any, TypeId};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use agenter::{DynTool, Tool, ToolError};

use crate::errors::{DomainError, Error};
use crate::tool_views::ToolRender;

/// 一次运行的回答交给谁：运行开始时由会话与它处理的信件推导一次，决定运行上下文里怎么说、ask_user 接不接受（没有用户
/// 会回答就拒绝）。工具列表不随它变（只由分发规则决定，缓存前缀才稳定）。不入库。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Audience {
    /// 有用户在等：运行结束时推送
    User,
    /// 委派来的工作（ask_agent、report_back）：回答交回发起委派的 agent
    Agent,
    /// 没有人在等（触发器、运行中检查……）：要通知用户时 agent 自己 notify_user
    None,
}

/// 一次运行（会话的一次运行，从开始到结束或挂起）交给所有工具的上下文。每次运行新建一个，不和别的运行共用。
pub struct RunScope {
    pub agent_id: AgentId,
    /// agent 的工作目录（相对路径与 shell 的 cwd 以它为准）
    pub workspace: PathBuf,
    pub session_id: SessionId,
    pub audience: Audience,
    /// 别的领域的工具在这次运行里要记的状态，按类型各一份（[`RunScope::state`]），随运行一起丢掉。平台不知道里面是什么
    states: Mutex<HashMap<TypeId, Arc<dyn Any + Send + Sync>>>,
}

impl std::fmt::Debug for RunScope {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RunScope")
            .field("agent_id", &self.agent_id)
            .field("workspace", &self.workspace)
            .field("session_id", &self.session_id)
            .field("audience", &self.audience)
            .finish_non_exhaustive()
    }
}

impl RunScope {
    pub fn new(agent_id: AgentId, workspace: PathBuf, session_id: SessionId, audience: Audience) -> Self {
        Self { agent_id, workspace, session_id, audience, states: Mutex::default() }
    }

    /// 用户在等这次运行的回答：它发起的后台任务、委派的结局随之推送。
    pub fn user_waits(&self) -> bool {
        self.audience == Audience::User
    }

    /// 这次运行里类型 T 的那份状态（第一次取时是 T::default()）。用法同 axum 的 Extension：哪个领域的工具要跨调用记
    /// 东西（如文件工具记读过哪些文件），就定义自己的类型放在这里，别的领域看不到也不用知道。
    pub fn state<T: Default + Send + Sync + 'static>(&self) -> Arc<T> {
        let mut states = self.states.lock().expect("run states lock");
        let state = states.entry(TypeId::of::<T>()).or_insert_with(|| Arc::new(T::default())).clone();
        state.downcast::<T>().expect("states are keyed by their type")
    }
}

/// 内置工具读还是写（界面、get_agent 展示）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolAccess {
    Read,
    Write,
}

/// 一个内置工具自己声明的属性。加一个工具只改它自己的定义：分发（读写、管理类）、消息落库前脱敏（secret_args）、界面
/// 展示（render）、执行方式（sequential）都从这里收集。
#[derive(Clone)]
pub struct ToolTraits {
    pub access: ToolAccess,
    /// 管理类工具（建、删 agent）：内置角色 base 不含，只经 admin 角色或显式规则给出
    pub admin: bool,
    /// 明文参数名：消息落库、推送前替换掉
    pub secret_args: &'static [&'static str],
    /// 界面上的展示形式；None 显示参数与结果的原文
    pub render: Option<Arc<ToolRender>>,
    /// 同一条回复里的调用中它是屏障：前面的调用（含 run_shell）做完才执行它，它做完才执行后面的（agenter 的
    /// `DynTool::sequential`）。文件工具用它（#224：模型常在一条回复里先用 run_shell 生成文件、再 read_file 它）
    pub sequential: bool,
}

impl ToolTraits {
    pub fn read() -> Self {
        Self::of(ToolAccess::Read)
    }

    pub fn write() -> Self {
        Self::of(ToolAccess::Write)
    }

    fn of(access: ToolAccess) -> Self {
        Self { access, admin: false, secret_args: &[], render: None, sequential: false }
    }

    pub fn admin(mut self) -> Self {
        self.admin = true;
        self
    }

    pub fn secret_args(mut self, names: &'static [&'static str]) -> Self {
        self.secret_args = names;
        self
    }

    pub fn render(mut self, render: ToolRender) -> Self {
        self.render = Some(Arc::new(render));
        self
    }

    pub fn sequential(mut self) -> Self {
        self.sequential = true;
        self
    }
}

/// 一个内置工具：执行（agenter 的工具）加上它的声明。只能经 [`builtin`] 得到。
#[derive(Clone)]
pub struct BuiltinTool {
    pub tool: Arc<dyn DynTool<RunScope>>,
    pub traits: ToolTraits,
}

impl BuiltinTool {
    pub fn name(&self) -> &str {
        &self.tool.def().name
    }
}

/// 把一个工具和它的声明交给运行端口的工具箱。声明里管执行的部分（sequential）在这里交给 agenter。
pub fn builtin<T: Tool<RunScope>>(tool: T, traits: ToolTraits) -> BuiltinTool {
    let tool = agenter::tool(tool);
    // 屏障只在声明里写，不在 Tool 上另写一份
    assert!(!tool.sequential(), "tool {:?}: declare sequential in its ToolTraits, not on the Tool", tool.def().name);
    let tool = if traits.sequential { Arc::new(Barrier(tool)) } else { tool };
    BuiltinTool { tool, traits }
}

/// 声明为屏障的工具（ToolTraits::sequential）。
struct Barrier(Arc<dyn DynTool<RunScope>>);

impl DynTool<RunScope> for Barrier {
    fn def(&self) -> &agenter::ToolDef {
        self.0.def()
    }

    fn sequential(&self) -> bool {
        true
    }

    fn call<'a>(
        &'a self,
        ctx: agenter::ToolCtx<'a, RunScope>,
        args: &'a str,
    ) -> futures::future::BoxFuture<'a, Result<agenter::ToolOutput, ToolError>> {
        self.0.call(ctx, args)
    }
}

/// 领域错误交还模型修正（见模块说明的统一规则）。
impl From<DomainError> for ToolError {
    fn from(e: DomainError) -> Self {
        ToolError::Retry(e.message)
    }
}

impl From<Error> for ToolError {
    fn from(e: Error) -> Self {
        match e {
            Error::Domain(d) => d.into(),
            e @ Error::Unavailable(_) => ToolError::Unavailable(Box::new(e)),
            other => ToolError::Abort(Box::new(other)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_type_has_one_state_per_run() {
        #[derive(Default)]
        struct Seen(Mutex<Vec<u32>>);
        let scope = RunScope::new(AgentId::new("a"), "/w".into(), SessionId::new("s"), Audience::None);
        scope.state::<Seen>().0.lock().unwrap().push(1);
        scope.state::<Seen>().0.lock().unwrap().push(2);
        assert_eq!(*scope.state::<Seen>().0.lock().unwrap(), [1, 2]);
        assert_eq!(*scope.state::<Mutex<u8>>().lock().unwrap(), 0, "another type starts from its default");
        let next = RunScope::new(AgentId::new("a"), "/w".into(), SessionId::new("s"), Audience::None);
        assert!(next.state::<Seen>().0.lock().unwrap().is_empty(), "a new run starts empty");
    }

    #[test]
    fn domain_errors_go_back_to_the_model() {
        let e: ToolError = DomainError::not_found("agent \"x\" not found").into();
        assert!(matches!(e, ToolError::Retry(m) if m == "agent \"x\" not found"));
    }
}
