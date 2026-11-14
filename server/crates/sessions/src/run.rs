//! 一次运行：把信箱里的信交给模型，一步一步走到模型不再调用工具（或停在挂起的调用上），每一步保存。设计见
//! docs/architecture.md「会话信箱」「每一步保存」「从哪里开始」。由会话 actor（[`crate::actor`]）调用，一次一个。
//!
//! - 起点由会话停在哪定（[`crate::pending::position`]）：从历史续跑（Resend）就重发末尾那条请求；被打断（Interrupted）就
//!   先补上「被中断、没有重新执行」的结果再看补完之后停在哪；等回答（Paused）且能继续时把回答作为挂起调用的结果交回；
//!   新开（Fresh）时把信箱里的信作为开头交给模型。
//! - 每一步保存：请求模型前，这条请求与它带上的信件（记为已处理）同一个事务提交；模型回复后提交回复；工具做完，结果与新到
//!   的信并成下一条请求再提交。失败或重启后从上一步续跑。停在挂起的调用上时，已经做完的工具结果先存成一条请求，继续时把
//!   它们连同回答交回 agenter，只把新出的结果接在后面存（库里的消息不改写）。
//! - 被用户停止：actor 发出停止信号，运行在等模型、等工具、压缩、组装运行处看到它就不再往下走（已经停止时不再开始新的
//!   请求或工具），把这一步已产生、还没保存的部分（生成到一半的回复、做完的工具结果）交回（[`Ran::Stopped`]），由停止事务
//!   保存。保存一步的事务不被打断。
//! - 收尾（[`finish_in`]）：记下运行的结局、结清做完的委派、发布 RunEnded。

use muliao_platform::ids::{MailId, RunId, SessionId};
use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::Arc;

use agenter::{
    DeltaKind, Event, Message, Request, RequestPart, Resolution, ResponseBuilder, ResponsePart, RunEvent, ToolCall,
    ToolResult, ToolsDone,
};
use chrono::Utc;
use muliao_assets::agents::agent_in;
use muliao_assets::run_assets::RunAssets;
use muliao_assets::service::AgentService;
use muliao_files::FileStore;
use muliao_platform::bus::Bus;
use muliao_platform::db::{Database, Tx};
use muliao_platform::tool_views::ToolRender;
use muliao_platform::tooling::Audience;
use muliao_runtime::instructions::with_notices;
use muliao_runtime::toolbox::Redaction;
use muliao_runtime::{Run, RunSpec, Runtime, ToolsRun};
use rusqlite::Connection;
use tokio_util::sync::CancellationToken;

use crate::actor::Waker;
use crate::answers::{Waiting, approvals_waiting_text, questions_of, questions_waiting_text};
use crate::compaction::{Compactor, Verdict};
use crate::content::{Content, append_content, content_of, request_of};
use crate::conventions::{
    MadeUp, answer_text, attach_run_context, interrupted_response, is_stopped, made_up_result, results_request,
    set_finished_at, shared_attachments, unanswered_calls,
};
use crate::directory::{self, Session};
use crate::events::{Events, RunEnd, RunEnded};
use crate::history::{self, SessionContext, Stored};
use crate::live::{DeltaType, Live};
use crate::mailbox::{Mail, consume_in, consumed_by_in, give_in, queued_in};
use crate::origins::OriginNotes;
use crate::paused_calls::{Pause, record_in};
use crate::pending::{Paused, PendingCall, Position, deferred_results};
use crate::runs::{self, Outcome};
use crate::settlement::{delegated_in, settle_finished_in};
use crate::transcript::{call_item, result_item};
use crate::work::position_in;
use muliao_platform::errors::{Error, Result, broken};

/// 会话领域运行时共用的部件（组合根装配一次）。
pub struct Core {
    pub db: Arc<Database>,
    pub runtime: Arc<Runtime>,
    pub files: Arc<FileStore>,
    pub agents: Arc<AgentService>,
    pub live: Live,
    pub events: Arc<Events>,
    pub notes: OriginNotes,
    pub waker: Waker,
    pub compactor: Compactor,
    pub renders: HashMap<String, Arc<ToolRender>>,
    pub redaction: Redaction,
    pub tz: chrono_tz::Tz,
    pub bus: Arc<Bus>,
}

/// 一次运行怎么结束的（失败不在这里，是 [`Failure`]）。
pub(crate) enum Ran {
    /// 走到了结局，已经收尾
    Finished,
    /// 看到停止信号停下：没有收尾（运行还没结束），已产生、没保存的消息（存储形态）交给停止事务
    Stopped(Vec<Message>),
}

/// 驱动没走到结局就停下了：失败，或被停止（带着这一步已产生、还没保存的消息）。用 `?` 一路传回 [`execute`]。
enum Cut {
    Failed(Failure),
    Stopped(Vec<Message>),
}

impl From<Failure> for Cut {
    fn from(f: Failure) -> Self {
        Cut::Failed(f)
    }
}

impl From<Error> for Cut {
    fn from(e: Error) -> Self {
        Cut::Failed(e.into())
    }
}

impl From<muliao_runtime::StepError> for Cut {
    fn from(e: muliao_runtime::StepError) -> Self {
        Cut::Failed(e.into())
    }
}

/// 运行失败：暂时性的（模型或外部服务一时不可用）由 actor 退避重试，其余最终失败。
#[derive(Debug)]
pub(crate) enum Failure {
    Transient(String),
    Final(String),
}

impl Failure {
    fn of(transient: bool, message: String) -> Self {
        if transient { Failure::Transient(message) } else { Failure::Final(message) }
    }

    pub fn message(&self) -> &str {
        match self {
            Failure::Transient(m) | Failure::Final(m) => m,
        }
    }
}

impl From<Error> for Failure {
    fn from(e: Error) -> Self {
        Failure::of(e.is_transient(), e.to_string())
    }
}

impl From<muliao_runtime::StepError> for Failure {
    fn from(e: muliao_runtime::StepError) -> Self {
        Failure::of(e.is_transient(), e.to_string())
    }
}

/// 回答交给谁（唯一规则）：处理的信件有用户在等的就是用户；否则会话有委派来、还没结清的工作就是发起委派的 agent；都没有
/// 就是没人在等。
pub(crate) fn audience_of(mail: &[&Mail], delegated: bool) -> Audience {
    if mail.iter().any(|m| m.awaited) {
        Audience::User
    } else if delegated {
        Audience::Agent
    } else {
        Audience::None
    }
}

/// 停在这些挂起的调用上：提问的问题、等批准的调用要告诉用户。
fn paused_end(calls: &[PendingCall]) -> RunEnd {
    let mut texts = Vec::new();
    let questions: Vec<_> =
        calls.iter().filter(|c| c.kind == Waiting::AskUser).flat_map(|c| questions_of(&c.call.args)).collect();
    if !questions.is_empty() {
        texts.push(questions_waiting_text(&questions));
    }
    let gated: Vec<String> = calls.iter().filter(|c| c.kind == Waiting::Approval).map(|c| brief(&c.call)).collect();
    if !gated.is_empty() {
        texts.push(approvals_waiting_text(&gated));
    }
    let waiting: BTreeSet<Waiting> = calls.iter().map(|c| c.kind).collect();
    RunEnd::Paused { prompt: texts.join("\n"), waiting_on: waiting.into_iter().collect() }
}

/// 通知里等批准的调用：工具名与参数（长的截短，全文在会话里）。
fn brief(call: &ToolCall) -> String {
    let args = if call.args.chars().count() <= 300 {
        call.args.clone()
    } else {
        format!("{}…", call.args.chars().take(300).collect::<String>())
    };
    format!("{} {args}", call.name)
}

/// 运行开头读出的状态。
struct Begun {
    run_id: RunId,
    session: Session,
    agent_name: String,
    context: SessionContext,
    queued: Vec<Mail>,
    at: Position,
    audience_mail: Vec<Mail>,
    delegated: bool,
    /// 接着别的运行保存的部分续跑时，那次运行处理过的信件：这次运行接管它们的推送
    adopted: Vec<Mail>,
    /// 之前的尝试已经跑完、只差收尾时它的结局（它保存的就是历史的末尾，且没标停止）
    settled: Option<RunEnd>,
}

/// 一次运行（或从上次的尝试接着跑）。Finished：运行已经收尾（结局已记下）；Stopped：看到 stop 停下、交出没保存的部分
/// （由 actor 执行停止事务）。Err 是失败，由 actor 决定重试还是最终失败。
pub(crate) async fn execute(
    core: &Core,
    session_id: &SessionId,
    stop: &CancellationToken,
) -> std::result::Result<Ran, Failure> {
    let begun = begin(core, session_id).await?;
    let start = match plan(core, &begun).await? {
        Plan::Drive(start) => start,
        Plan::Settle(end) => {
            finish(core, &begun, Ending::Ended(end)).await?;
            return Ok(Ran::Finished);
        }
        Plan::Idle => {
            finish(core, &begun, Ending::Idle).await?;
            return Ok(Ran::Finished);
        }
    };
    let assets = core.agents.run_assets(&begun.session.agent_id).await?;
    let audience = audience_of(&begun.audience_mail.iter().collect::<Vec<_>>(), begun.delegated);
    let mut driver = Driver {
        core,
        begun: &begun,
        assets: &assets,
        audience,
        stop,
        history: begun.context.messages.clone(),
        summary: begun.context.summary.clone(),
        stale_responses: begun.context.stale_responses,
        run_context_row: None,
        compaction_exhausted: false,
    };
    match driver.drive(start).await {
        Ok(end) => {
            finish(core, &begun, Ending::Ended(end)).await?;
            Ok(Ran::Finished)
        }
        Err(Cut::Stopped(messages)) => Ok(Ran::Stopped(messages)),
        Err(Cut::Failed(f)) => Err(f),
    }
}

/// 开头的事务：取（或接着）这次运行的记录，读出状态；被打断的那一步补上「被中断」的结果。
async fn begin(core: &Core, session_id: &SessionId) -> Result<Begun> {
    let (session_id, live) = (session_id.to_owned(), core.live.clone());
    core.db
        .write(move |tx| {
            let session = directory::running_in(tx, &session_id)?;
            let run_id = runs::current_or_start_in(tx, &live, &session_id)?;
            let queued = queued_in(tx, &session_id)?;
            // 信箱里已有的信交给这次运行，哪怕它在交给模型之前就最终失败：失败的结清据此了结它们带来的委派，也不会为它们再唤醒
            give_in(tx, &queued.iter().map(|m| m.id).collect::<Vec<_>>(), run_id)?;
            let queued = queued_in(tx, &session_id)?;
            let mut at = position_in(tx, &session_id)?;
            if let Position::Interrupted { calls, after } = at {
                // 被打断的那一步里没出结果的调用不重新执行——副作用可能已经发生，重放一条重启服务的命令会让恢复无限循环
                tracing::warn!(%session_id, %run_id, tools = ?calls.iter().map(|c| &c.name).collect::<Vec<_>>(),
                    "resuming an interrupted step; its unfinished tool calls are not re-executed");
                let results = calls.iter().map(|c| made_up_result(c, MadeUp::Interrupted)).collect();
                let request = results_request(results).expect("interrupted calls exist");
                history::append_in(tx, &session_id, run_id, &[Message::User(request)])?;
                at = after.into();
            }
            let resuming = matches!(at, Position::Resend);
            let adopted = if resuming { adopted_in(tx, &session_id, run_id)? } else { Vec::new() };
            let earlier = consumed_by_in(tx, &session_id, run_id)?;
            let saved = history::saved_by_in(tx, &session_id, run_id)?;
            let last = history::last_message_id_in(tx, &session_id)?;
            let context = history::load_context(tx, &session_id)?;
            let settled = if saved.last().is_some_and(|s: &Stored| Some(s.id) == last && !is_stopped(&s.message)) {
                settled_end(&at, &context)
            } else {
                None
            };
            let agent_name = agent_in(tx, &session.agent_id)?.name;
            let mut audience_mail = earlier;
            audience_mail.extend(adopted.iter().cloned());
            audience_mail.extend(queued.iter().cloned());
            Ok(Begun {
                run_id,
                context,
                delegated: delegated_in(tx, &session_id)?,
                agent_name: agent_name.to_string(),
                session,
                queued,
                at,
                audience_mail,
                adopted,
                settled,
            })
        })
        .await
}

/// 从历史中途续跑、而停下的那部分是另一次运行保存的（它失败了，之后有新信件唤醒了这次运行）：这次运行接着做完它的工作，
/// 也接管它处理过的信件——推送由这次运行做。
fn adopted_in(c: &Connection, session_id: &SessionId, run_id: RunId) -> Result<Vec<Mail>> {
    match history::last_saved_by_in(c, session_id)? {
        Some(tail) if tail != run_id => consumed_by_in(c, session_id, tail),
        _ => Ok(Vec::new()),
    }
}

/// 这次运行开头要做什么。
enum Plan {
    /// 交给模型
    Drive(Start),
    /// 没有要交给模型的，之前的尝试已经跑完、只差收尾：记下它的结局
    Settle(RunEnd),
    /// 没有要交给模型的：信箱空了，或停在挂起的调用上、还不能继续
    Idle,
}

/// 交给模型时从哪开始。mail 是开头取走的信件（随这次运行新增的第一条请求记为已处理）。
enum Start {
    /// 从历史续跑：重发末尾那条请求
    Resend,
    /// 停在挂起的调用上、能继续：回答作为挂起调用的结果交回，其余信件作为内容
    Resume { paused: Paused, content: Content, mail: Vec<MailId> },
    /// 新开：信箱里的信作为开头的内容，自己成一条请求
    Fresh { content: Content, mail: Vec<MailId> },
}

async fn plan(core: &Core, begun: &Begun) -> Result<Plan> {
    let workspace = core.agents.workspace(&begun.session.agent_id);
    let mail = || begun.queued.iter().map(|m| m.id).collect();
    let start = match &begun.at {
        Position::Resend => Some(Start::Resend),
        Position::Paused(p) if p.ready => {
            let answering: HashSet<MailId> = p.answer_mail.iter().map(|(id, _)| *id).collect();
            let rest: Vec<Mail> = begun.queued.iter().filter(|m| !answering.contains(&m.id)).cloned().collect();
            Some(Start::Resume {
                paused: p.clone(),
                content: content_of(&core.files, &workspace, &rest).await?,
                mail: mail(),
            })
        }
        Position::Fresh => {
            let content = content_of(&core.files, &workspace, &begun.queued).await?;
            (!content.is_empty()).then(|| Start::Fresh { content, mail: mail() })
        }
        // 被打断的那一步开头已补上结果、换成了补完之后停在哪
        Position::Paused(_) | Position::Idle | Position::Interrupted { .. } => None,
    };
    Ok(match (start, &begun.settled) {
        (Some(start), _) => Plan::Drive(start),
        (None, Some(end)) => Plan::Settle(end.clone()),
        (None, None) => Plan::Idle,
    })
}

/// 历史的末尾是之前的尝试保存的：它停在挂起的调用上或以模型的回答结束时就是已经跑完、只差收尾，得出它怎么结束的；末尾是
/// 一条请求时没跑完（从历史续跑）。
fn settled_end(at: &Position, context: &SessionContext) -> Option<RunEnd> {
    if let Some(p) = at.paused() {
        return Some(paused_end(&p.pending));
    }
    match context.messages.last() {
        Some(Message::Assistant(r)) => Some(RunEnd::Answered { answer: answer_text(r) }),
        Some(Message::User(_)) | None => None,
    }
}

/// 收尾时这次运行的结局：没事可做的空收尾只记结局，否则还要结清、发布 RunEnded。
enum Ending {
    Idle,
    Ended(RunEnd),
}

/// 收尾：记下结局、结清做完的委派、发布 RunEnded（[`Ending`]）。
async fn finish(core: &Core, begun: &Begun, end: Ending) -> Result<()> {
    let finishing = Finishing {
        run_id: begun.run_id,
        session_id: begun.session.id.clone(),
        agent_name: begun.agent_name.clone(),
        adopted: begun.adopted.clone(),
    };
    let (waker, events, live) = (core.waker.clone(), core.events.clone(), core.live.clone());
    core.db.write(move |tx| finish_in(tx, &waker, &events, &live, &finishing, end)).await
}

/// 收尾要的这次运行的身份（从 [`Begun`] 拷出，交给写线程）。
struct Finishing {
    run_id: RunId,
    session_id: SessionId,
    agent_name: String,
    adopted: Vec<Mail>,
}

fn finish_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    events: &Events,
    live: &Live,
    finishing: &Finishing,
    end: Ending,
) -> Result<()> {
    let Finishing { run_id, session_id, agent_name, adopted } = finishing;
    let run_id = *run_id;
    runs::end_in(tx, live, run_id, Outcome::Done)?;
    let Ending::Ended(end) = end else { return Ok(()) };
    let saved = history::saved_by_in(tx, session_id, run_id)?;
    if saved.is_empty() {
        // 交回挂起调用的结局后又停在挂起的调用上、这一步什么也没存：没有新东西要结清、告诉谁
        return Ok(());
    }
    let session = directory::running_in(tx, session_id)?;
    directory::touch_in(tx, session_id)?;
    let mut handled = consumed_by_in(tx, session_id, run_id)?;
    handled.extend(adopted.iter().cloned());
    // 工作做完了：没有人替它在外面做事，信箱里也没有还要交给模型的信（比如最后一次请求期间才到的结局，它会唤醒下一次运行，
    // 结清由那次运行做）
    let done = !crate::holds::has_active_in(tx, session_id)? && queued_in(tx, session_id)?.is_empty();
    if let RunEnd::Answered { answer } = &end
        && done
    {
        let messages: Vec<Message> = saved.into_iter().map(|s| s.message).collect();
        let shared = shared_attachments(&messages);
        settle_finished_in(tx, waker, &session, answer, &shared)?;
    }
    // 最后发布：订阅者（推送、issue 的表态）看到的是结清之后的状态
    events.run_ended(
        tx,
        &RunEnded {
            session_id: session_id.to_owned(),
            session_title: session.title.clone(),
            origin: session.origin,
            agent_id: session.agent_id.clone(),
            agent_name: agent_name.to_owned(),
            run_id,
            end,
            awaited: handled.iter().any(|m| m.awaited),
        },
    )
}

/// 驱动一次运行的状态（只在这次运行里用）。
struct Driver<'a> {
    core: &'a Core,
    begun: &'a Begun,
    assets: &'a RunAssets,
    audience: Audience,
    /// 停止信号：等模型、等工具、压缩、组装运行时看它，保存一步时不看
    stop: &'a CancellationToken,
    /// 发给模型的历史（存储形态）：保存一步就接上这一步，压缩后从库里重新加载。这次运行新增的工具调用不脱敏（库里那份
    /// 脱敏）：下一步执行的就是它们（#770）
    history: Vec<Message>,
    /// 最近一次压缩的摘要（进指令），和 history 一起由加载上下文得出、压缩后一起重载
    summary: Option<String>,
    /// 见 SessionContext::stale_responses
    stale_responses: Option<usize>,
    /// 挂着这次运行的运行上下文的那条请求：还没挂，或压缩把它切进了摘要时为 None，下一条新请求挂一段
    run_context_row: Option<i64>,
    /// 压缩不下去了：这次运行不再因为上下文大小停下
    compaction_exhausted: bool,
}

/// 驱动的一步之后怎么走。
enum Next {
    Continue,
    End(RunEnd),
}

impl Driver<'_> {
    fn session_id(&self) -> &SessionId {
        &self.begun.session.id
    }

    /// fut 做完之前被停止就丢掉它（None）。先看信号：已经停止就不再开始（一次也不轮询 fut）。
    async fn unless_stopped<T>(&self, fut: impl Future<Output = T>) -> Option<T> {
        tokio::select! {
            biased;
            () = self.stop.cancelled() => None,
            r = fut => Some(r),
        }
    }

    async fn start_run(&self) -> std::result::Result<Arc<Run>, Cut> {
        let spec = RunSpec {
            assets: self.assets,
            session_id: self.session_id(),
            audience: self.audience,
            origin_note: self.core.notes.of(self.begun.session.origin.origin()),
            summary: self.summary.as_deref(),
        };
        let Some(run) = self.unless_stopped(self.core.runtime.start(spec)).await else {
            return Err(Cut::Stopped(Vec::new()));
        };
        Ok(Arc::new(run?))
    }

    async fn drive(&mut self, start: Start) -> std::result::Result<RunEnd, Cut> {
        // 上下文超过阈值时先压缩：放在确认有要交给模型的之后、交出信件与批准之前
        if self.compaction_due() {
            self.compact().await?;
        }
        let mut run = self.start_run().await?;
        let mut notices: Vec<String> = run.notices().to_vec();
        let session_id = self.session_id().to_owned();
        let last = self.core.db.read(move |c| history::last_message_id_in(c, &session_id)).await?;
        // 实时展示随这个守卫结束：运行返回、被停止时丢掉、panic 时都一样
        let _live = self.core.live.begin(self.session_id(), last);
        self.steps(&mut run, &mut notices, start).await
    }

    async fn steps(
        &mut self,
        run: &mut Arc<Run>,
        notices: &mut Vec<String>,
        start: Start,
    ) -> std::result::Result<RunEnd, Cut> {
        // 挂起调用的结局（停在挂起的调用上、能继续时）
        let mut resolved: Option<HashMap<String, Resolution>> = None;
        // 继续挂起的调用时开头的内容与信件：并进这次运行新增的第一条请求（随交回的结果）
        let mut first: Option<(Content, Vec<MailId>)> = None;
        match start {
            Start::Resend => {}
            Start::Resume { paused, content, mail } => {
                resolved = Some(self.hand_over(&paused).await?);
                first = Some((content, mail));
            }
            Start::Fresh { content, mail } => {
                self.save_request(request_of(content), mail, notices).await?;
                if let Some(r) = self.after_request(notices).await? {
                    *run = r;
                }
            }
        }
        loop {
            // 下一步看最后一条回复里还有没有没出结果的调用，不看末尾消息的类型：停在挂起的调用上时，已做完的结果存在
            // 回复之后，末尾是一条请求，可还有调用在等结局
            let open = !unanswered_calls(&self.history)?.is_empty();
            let next = match self.history.last() {
                None => return Err(broken("cannot run from an empty history").into()),
                _ if open => self.tools(run, resolved.take(), first.take(), notices).await?,
                Some(Message::User(_)) => self.request(run).await?,
                Some(Message::Assistant(r)) => Next::End(RunEnd::Answered { answer: answer_text(r) }),
            };
            match next {
                Next::End(end) => return Ok(end),
                Next::Continue => {}
            }
            if matches!(self.history.last(), Some(Message::User(_)))
                && let Some(r) = self.after_request(notices).await?
            {
                *run = r;
            }
        }
    }

    /// 停在挂起的调用上、能继续：开头取走的信件交给这次运行，被批准的调用记为已交给运行执行（之前交过的不再执行，给「被
    /// 中断」）；被问的 agent 挂给用户的文件随它的结局交回（成为这次调用结果的 metadata）。
    ///
    /// 「交给过运行」单独一个事务提交（Python 与取走信件同一事务）：之后到这一步保存之间失败的，重试时这个批准给「被中断」、
    /// 不执行——偏保守，不会重复执行，窗口只有交出回答到工具开始执行之间。
    async fn hand_over(&self, p: &Paused) -> Result<HashMap<String, Resolution>> {
        let (session_id, answers) = (self.session_id().to_owned(), p.answers.clone());
        let decided = self.core.db.write(move |tx| crate::paused_calls::take_in(tx, &session_id, &answers)).await?;
        deferred_results(p, decided)
    }

    /// 历史末尾是用户一方的消息：请求模型，保存回复。生成到一半被停止时交出已生成的部分。
    async fn request(&mut self, run: &Run) -> std::result::Result<Next, Cut> {
        let mut streamed = ResponseBuilder::new();
        let responded = {
            let mut live = self.on_event();
            let mut on_event = |event: RunEvent<'_>| {
                if let RunEvent::Model(e) = &event {
                    let _ = streamed.apply(e);
                }
                live(event)
            };
            self.unless_stopped(run.respond(&self.history, &mut on_event)).await
        };
        let Some(response) = responded else {
            let parts = streamed.into_parts();
            let unsaved = if parts.is_empty() {
                Vec::new()
            } else {
                vec![Message::Assistant(interrupted_response(parts, "", ""))]
            };
            return Err(Cut::Stopped(unsaved));
        };
        self.save(vec![Message::Assistant(response?)], Vec::new()).await?;
        Ok(Next::Continue)
    }

    /// 最后一条回复里还有没出结果的调用：执行工具（或交回挂起调用的结局）。
    async fn tools(
        &mut self,
        run: &Run,
        resolved: Option<HashMap<String, Resolution>>,
        first: Option<(Content, Vec<MailId>)>,
        notices: &mut Vec<String>,
    ) -> std::result::Result<Next, Cut> {
        // 末尾那条回复（之后可能跟着挂起前存下的结果）
        let (at, response) = self
            .history
            .iter()
            .enumerate()
            .rev()
            .find_map(|(i, m)| match m {
                Message::Assistant(r) => Some((i, r)),
                Message::User(_) => None,
            })
            .expect("ends with a response");
        // 挂起之前已经做完、存在回复之后的结果：连同回答交回 agenter（它们不再执行），新出的结果只存新的那些
        let saved: Vec<ToolResult> = self.history[at + 1..]
            .iter()
            .filter_map(|m| match m {
                Message::User(r) => Some(r),
                Message::Assistant(_) => None,
            })
            .flat_map(|r| &r.parts)
            .filter_map(|p| match p {
                RequestPart::ToolResult(t) => Some(t.clone()),
                _ => None,
            })
            .collect();
        let mut resolved = resolved.unwrap_or_default();
        for t in &saved {
            resolved.insert(t.call_id.clone(), Resolution::from(t.clone()));
        }
        let already: HashSet<String> = saved.iter().map(|t| t.call_id.clone()).collect();
        // 做完的调用各自的结束时刻
        let mut finished_at: HashMap<String, chrono::DateTime<Utc>> = HashMap::new();
        let done = {
            let mut live = self.on_event();
            let mut on_event = |event: RunEvent<'_>| {
                if let RunEvent::ToolFinished(r) = &event {
                    finished_at.insert(r.call_id.clone(), Utc::now());
                }
                live(event)
            };
            run.run_tools(&self.history[..at], response, &resolved, self.stop.cancelled(), &mut on_event).await?
        };
        let fresh = |results: Vec<ToolResult>| -> Vec<ToolResult> {
            results
                .into_iter()
                .filter(|t| !already.contains(&t.call_id))
                .map(|mut t| {
                    let at = finished_at.get(&t.call_id).copied().unwrap_or_else(Utc::now);
                    set_finished_at(&mut t, at);
                    t
                })
                .collect()
        };
        match done {
            // 停在工具还没做完时：做完的结果交给停止事务，没做完的由它补「被停止」
            ToolsRun::Stopped(results) => {
                Err(Cut::Stopped(results_request(fresh(results)).map(Message::User).into_iter().collect()))
            }
            ToolsRun::Done(ToolsDone::Results(results)) => {
                let results = fresh(results);
                let mut request = results_request(results).unwrap_or_else(|| Request {
                    parts: Vec::new(),
                    timestamp: Utc::now(),
                    metadata: None,
                });
                let mail = match first {
                    Some((content, mail)) => {
                        append_content(&mut request, content);
                        mail
                    }
                    None => Vec::new(),
                };
                self.save_request(request, mail, notices).await?;
                Ok(Next::Continue)
            }
            ToolsRun::Done(ToolsDone::Paused { results, approvals, .. }) => {
                // 停在挂起的调用上：已做完的结果与这次请求批准的调用同一个事务记下（分开提交时，进程在两者之间退出会留下
                // 一个没有结果、又不算在等批准的调用）。交回 Defer 的调用由工具自己在挂起前记过；停在哪些调用上只问 position
                let requested: Vec<(String, Pause)> =
                    approvals.into_iter().map(|(c, pin)| (c.id, Pause::Approval { pin })).collect();
                let mut request = results_request(fresh(results));
                // 继续之后又停在挂起的调用上：开头取走的信也在这一步记为已处理，内容随结果存下（下次随它们交给模型）
                let mail = match first {
                    Some((content, mail)) => {
                        let r = request.get_or_insert_with(|| Request {
                            parts: Vec::new(),
                            timestamp: Utc::now(),
                            metadata: None,
                        });
                        append_content(r, content);
                        mail
                    }
                    None => Vec::new(),
                };
                let new: Vec<Message> =
                    request.filter(|r| !r.parts.is_empty()).map(Message::User).into_iter().collect();
                let session_id = self.session_id().to_owned();
                self.save_with(new, mail, move |tx| {
                    requested.iter().try_for_each(|(call_id, pause)| record_in(tx, &session_id, call_id, pause))
                })
                .await?;
                let session_id = self.session_id().to_owned();
                let at = self.core.db.read(move |c| position_in(c, &session_id)).await?;
                // 停下了却没有挂起的调用：有工具交回 Defer 却没记下挂起
                let Some(p) = at.paused() else { return Err(broken("run paused on calls nobody recorded").into()) };
                Ok(Next::End(paused_end(&p.pending)))
            }
        }
    }

    /// 一条新请求（这次运行新增的）：并进新到的信与运行上下文，与它带上的信件同一个事务保存。
    async fn save_request(
        &mut self,
        mut request: Request,
        mut mail: Vec<MailId>,
        notices: &mut Vec<String>,
    ) -> Result<()> {
        let session_id = self.session_id().to_owned();
        let queued = self.core.db.read(move |c| queued_in(c, &session_id)).await?;
        let arrived: Vec<Mail> = queued.into_iter().filter(|m| !mail.contains(&m.id)).collect();
        if !arrived.is_empty() {
            let workspace = self.core.agents.workspace(&self.begun.session.agent_id);
            // 运行中途没有挂起的调用（调用只在运行结束时挂起），这时到达的回答都是迟到的
            append_content(&mut request, content_of(&self.core.files, &workspace, &arrived).await?);
            mail.extend(arrived.iter().map(|m| m.id));
        }
        let attaching = self.run_context_row.is_none();
        let run_context = if attaching { self.core.runtime.run_context(self.audience) } else { String::new() };
        let context = with_notices(&run_context, notices);
        if !context.is_empty() {
            // 只挂在本次运行新增的第一条请求上（重发的那条已经在库里，不改它）；运行上下文已经挂出去了时，notices 单独挂
            attach_run_context(&mut request, &context);
        }
        let ids = self.save(vec![Message::User(request)], mail).await?;
        let id = *ids.last().expect("one request saved");
        if attaching {
            self.run_context_row = Some(id);
        }
        notices.clear();
        self.core.live.step(self.session_id(), id);
        Ok(())
    }

    /// 刚保存了一条请求：上下文超过阈值时压缩，从库里重新加载上下文续跑（新的摘要在指令里，所以重新组装这次运行）。
    async fn after_request(&mut self, notices: &mut Vec<String>) -> std::result::Result<Option<Arc<Run>>, Cut> {
        if !self.compaction_due() {
            return Ok(None);
        }
        self.compact().await?;
        let run = self.start_run().await?;
        notices.extend(run.notices().iter().cloned());
        Ok(Some(run))
    }

    fn compaction_due(&mut self) -> bool {
        if self.compaction_exhausted {
            return false;
        }
        match self.core.compactor.verdict(self.session_id(), &self.history, self.stale_responses) {
            Verdict::Due => true,
            Verdict::Hopeless => {
                self.compaction_exhausted = true;
                false
            }
            Verdict::NotDue => false,
        }
    }

    /// 压缩到一半被停止：没写完的这一批作废，已提交的批次保留。
    async fn compact(&mut self) -> std::result::Result<(), Cut> {
        let Some(compacted) = self.unless_stopped(self.core.compactor.compact(self.session_id(), self.assets)).await
        else {
            return Err(Cut::Stopped(Vec::new()));
        };
        let compacted = compacted?;
        let session_id = self.session_id().to_owned();
        let (context, through) = self
            .core
            .db
            .read(move |c| {
                let through = history::latest_compaction(c, &session_id)?.map(|x| x.through_message_id);
                Ok::<_, Error>((history::load_context(c, &session_id)?, through))
            })
            .await?;
        if let (Some(row), Some(through)) = (self.run_context_row, through)
            && row <= through
        {
            self.run_context_row = None;
        }
        self.history = context.messages;
        self.summary = context.summary;
        self.stale_responses = context.stale_responses;
        self.compaction_exhausted = compacted == 0;
        Ok(())
    }

    /// 保存这次运行的这些消息，并把它们带上的信件记为已处理。返回写下的消息 id。
    async fn save(&mut self, messages: Vec<Message>, mail: Vec<MailId>) -> Result<Vec<i64>> {
        self.save_with(messages, mail, |_| Ok(())).await
    }

    async fn save_with(
        &mut self,
        messages: Vec<Message>,
        mail: Vec<MailId>,
        also: impl FnOnce(&mut Tx<'_>) -> Result<()> + Send + 'static,
    ) -> Result<Vec<i64>> {
        // 脱敏只作用于存下的那份；这次运行内存里的历史留明文：下一步执行的就是这些调用，执行时要拿到真的参数（#770）
        let mut stored = messages.clone();
        self.core.redaction.messages(&mut stored);
        let (session_id, run_id) = (self.session_id().to_owned(), self.begun.run_id);
        let ids = self
            .core
            .db
            .write(move |tx| {
                let ids = history::append_in(tx, &session_id, run_id, &stored)?;
                consume_in(tx, &mail, run_id)?;
                also(tx)?;
                tracing::info!(%session_id, %run_id, messages = ids.len(), consumed_mail = ?mail, "step saved");
                Ok::<_, Error>(ids)
            })
            .await?;
        self.history.extend(messages);
        Ok(ids)
    }

    /// 给界面的实时事件。
    fn on_event(&self) -> impl FnMut(RunEvent<'_>) + Send + use<> {
        let live = self.core.live.clone();
        let (session_id, agent) = (self.session_id().to_owned(), self.begun.agent_name.clone());
        let renders = self.core.renders.clone();
        move |event| match event {
            RunEvent::Model(e) => match e {
                Event::PartStart { part: ResponsePart::Text { text, .. }, .. } => {
                    live.delta(&session_id, &agent, DeltaType::Assistant, text)
                }
                Event::PartStart { part: ResponsePart::Thinking { text, .. }, .. } => {
                    live.delta(&session_id, &agent, DeltaType::Thinking, text)
                }
                Event::Delta { kind: DeltaKind::Text, text, .. } => {
                    live.delta(&session_id, &agent, DeltaType::Assistant, text)
                }
                Event::Delta { kind: DeltaKind::Thinking, text, .. } => {
                    live.delta(&session_id, &agent, DeltaType::Thinking, text)
                }
                _ => {}
            },
            RunEvent::ToolStarted(call) => live.item(&session_id, &agent, call_item(call, Some(Utc::now()), &renders)),
            RunEvent::ToolFinished(result) => {
                let mut shown = result.clone();
                set_finished_at(&mut shown, Utc::now());
                match result_item(&shown, &renders) {
                    Ok(item) => live.item(&session_id, &agent, item),
                    // 只是实时展示：这一条不显示，保存后历史里照常报出
                    Err(e) => tracing::error!(%session_id, error = %e, "a tool result cannot be shown live"),
                }
            }
        }
    }
}
