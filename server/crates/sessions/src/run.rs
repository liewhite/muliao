//! 一次运行：把信箱里的信交给模型，一步一步走到模型不再调用工具（或停在挂起的调用上），每一步保存。设计见
//! docs/architecture.md「会话信箱」「每一步保存」「从哪里开始」。由会话 actor（[`crate::actor`]）调用，一次一个。
//!
//! - 起点由会话停在哪定（[`crate::pending::position`]）：从历史续跑（Resend）就重发末尾那条请求、或重新执行能安全重放的
//!   调用；被打断（Interrupted）就先补上「被中断、没有重新执行」的结果再看补完之后停在哪；等回答（Paused）且能继续时把
//!   回答作为挂起调用的结果交回；新开（Fresh）时把信箱里的信作为开头交给模型。
//! - 每一步保存：请求模型前，这条请求与它带上的信件（记为已处理）同一个事务提交；模型回复后提交回复；工具做完，结果与新到
//!   的信并成下一条请求再提交。失败或重启后从上一步续跑。停在挂起的调用上时，已经做完的工具结果先存成一条请求，继续时把
//!   它们连同回答交回 agenter，只把新出的结果接在后面存（库里的消息不改写）。
//! - 被用户停止时 actor 丢掉这次运行，已产生的部分（生成到一半的回复、做完的工具结果）在 [`Partial`] 里，由停止事务保存。
//! - 收尾（[`finish_in`]）：记下运行的结局、结清做完的委派、发布 RunEnded。

use std::collections::{BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use agenter::{
    DeltaKind, Event, Message, Request, RequestPart, Resolution, ResponseBuilder, ResponsePart, RunEvent, Step,
    ToolCall, ToolResult,
};
use chrono::Utc;
use muliao_assets::agents::agent_in;
use muliao_assets::service::AgentService;
use muliao_assets::tree::AgentDir;
use muliao_files::FileStore;
use muliao_platform::bus::Bus;
use muliao_platform::db::{Database, Tx};
use muliao_platform::tool_views::ToolRender;
use muliao_platform::tooling::Audience;
use muliao_runtime::conventions::{attach, shared_attachments};
use muliao_runtime::instructions::with_notices;
use muliao_runtime::toolbox::Redaction;
use muliao_runtime::{Run, RunSpec, Runtime};
use rusqlite::Connection;

use crate::actor::Waker;
use crate::answers::{CallResult, Waiting, approvals_waiting_text, questions_of, questions_waiting_text};
use crate::compaction::{Compactor, Verdict};
use crate::content::{Content, append_content, content_of, request_of};
use crate::conventions::{
    MadeUp, answer_text, attach_run_context, interrupted_response, is_stopped, made_up_result, results_request,
    set_finished_at, unanswered_calls,
};
use crate::directory::{self, Session};
use crate::error::{Error, Result, broken};
use crate::events::{Events, RunEnded};
use crate::history::{self, SessionContext, Stored};
use crate::live::{DeltaType, Live};
use crate::mailbox::{Mail, consume_in, consumed_by_in, give_in, queued_in};
use crate::origins::OriginNotes;
use crate::pending::{Paused, PendingCall, Position, deferred_results, paused_calls};
use crate::runs::{self, Outcome, RunKind};
use crate::settlement::{delegated_in, settle_finished_in};
use crate::transcript::{call_item, result_item};
use crate::work::position_in;

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

/// 一次运行已产生、还没保存的部分（被停止时由停止事务保存）。保存一步的事务在写线程上取走它对应的部分，所以停止事务（排在
/// 后面）不会再存一遍。运行记录建好之后才有（[`PartialSlot`]）。
pub(crate) struct Partial {
    /// 这次运行（被停止时，停止事务把补的结果记在它名下）
    run_id: i64,
    /// 生成到一半的回复
    pub streaming: Option<ResponseBuilder>,
    /// 这一步里已经做完的工具调用（原样）
    pub finished: Vec<ToolResult>,
    /// 这一步里已做完调用的结束时刻
    pub finished_at: HashMap<String, chrono::DateTime<Utc>>,
    /// 驱动这次运行的运行端口（存储形态的转换要用它）
    pub run: Option<Arc<Run>>,
}

pub(crate) type SharedPartial = Arc<Mutex<Partial>>;

/// 一次尝试的已产生部分的登记处（actor 停止时从这里取）：开头的事务建好运行记录才登记，没登记的尝试没有要保存的。
pub(crate) type PartialSlot = Arc<Mutex<Option<SharedPartial>>>;

/// 被停止时这次运行已产生、还没保存的部分（存储形态）：记在哪次运行名下、哪些消息。
pub(crate) struct Unsaved {
    pub run_id: i64,
    pub messages: Vec<Message>,
}

/// 运行失败：暂时性的（模型或外部服务一时不可用）由 actor 退避重试，其余最终失败。
#[derive(Debug)]
pub(crate) struct Failure {
    pub transient: bool,
    pub message: String,
}

impl From<Error> for Failure {
    fn from(e: Error) -> Self {
        Failure { transient: e.is_transient(), message: e.to_string() }
    }
}

impl From<muliao_runtime::StepError> for Failure {
    fn from(e: muliao_runtime::StepError) -> Self {
        Failure { transient: e.is_transient(), message: e.to_string() }
    }
}

impl From<muliao_runtime::StartError> for Failure {
    fn from(e: muliao_runtime::StartError) -> Self {
        Failure { transient: false, message: e.to_string() }
    }
}

impl From<muliao_assets::error::Error> for Failure {
    fn from(e: muliao_assets::error::Error) -> Self {
        Failure { transient: false, message: e.to_string() }
    }
}

/// 回答交给谁（唯一规则）：处理的信件有用户在等的就是用户；否则会话有委派来、还没结清的工作就是发起委派的 agent；都没有
/// 就是没人在等。
pub fn audience_of(mail: &[&Mail], delegated: bool) -> Audience {
    if mail.iter().any(|m| m.awaited) {
        Audience::User
    } else if delegated {
        Audience::Agent
    } else {
        Audience::None
    }
}

/// 一次运行怎么结束的。
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct RunEnd {
    /// 给用户的回答：停在提问、等批准上时是问题与等批准的调用
    pub answer: String,
    pub stopped: bool,
    pub waiting_on: Vec<Waiting>,
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
    RunEnd { answer: texts.join("\n"), stopped: false, waiting_on: waiting.into_iter().collect() }
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
    run_id: i64,
    session: Session,
    agent_name: String,
    context: SessionContext,
    queued: Vec<Mail>,
    at: Position,
    audience_mail: Vec<Mail>,
    delegated: bool,
    /// 接着别的运行保存的部分续跑时，那次运行处理过的信件：这次运行接管它们的推送
    adopted: Vec<Mail>,
    /// 之前的尝试已经跑完、只差收尾（它保存的就是历史的末尾，且没标停止）
    settled: bool,
}

/// 一次运行（或从上次的尝试接着跑）：返回时运行已经收尾（结局已记下）。Err 是失败，由 actor 决定重试还是最终失败。
pub(crate) async fn execute(core: &Core, session_id: &str, slot: &PartialSlot) -> std::result::Result<(), Failure> {
    let begun = begin(core, session_id).await?;
    let partial: SharedPartial = Arc::new(Mutex::new(Partial {
        run_id: begun.run_id,
        streaming: None,
        finished: Vec::new(),
        finished_at: HashMap::new(),
        run: None,
    }));
    *slot.lock().expect("partial slot lock") = Some(partial.clone());
    let opening = opening(core, &begun).await?;
    let resend = matches!(begun.at, Position::Resend { .. });
    if opening.is_none() && !resend {
        // 没有要交给模型的：信箱空了，或停在挂起的调用上、还不能继续
        let end = if begun.settled { Some(settled_end(&begun)?) } else { None };
        finish(core, &begun, end).await?;
        return Ok(());
    }
    let config = core.runtime.config(&begun.session.agent_id).await?;
    let audience = audience_of(&begun.audience_mail.iter().collect::<Vec<_>>(), begun.delegated);
    let mut driver = Driver {
        core,
        begun: &begun,
        config: &config,
        audience,
        partial,
        history: begun.context.messages.clone(),
        summary: begun.context.summary.clone(),
        stale_responses: begun.context.stale_responses,
        run_context_row: None,
        compaction_exhausted: false,
    };
    // 上下文超过阈值时先压缩：放在确认有要交给模型的之后、交出信件与批准之前
    if driver.compaction_due() {
        driver.compact().await?;
    }
    let end = driver.drive(opening).await?;
    finish(core, &begun, Some(end)).await?;
    Ok(())
}

/// 开头的事务：取（或接着）这次运行的记录，读出状态；被打断的那一步补上「被中断」的结果。
async fn begin(core: &Core, session_id: &str) -> Result<Begun> {
    let (session_id, live) = (session_id.to_owned(), core.live.clone());
    core.db
        .write(move |tx| {
            let session = directory::get_in(tx, &session_id)?.unwrap_or_else(|| {
                panic!("session {session_id} vanished while it had work: busy sessions are not deleted")
            });
            let run_id = match runs::unfinished_in(tx, &session_id)? {
                Some(r) => r.id,
                None => runs::start_in(tx, &live, &session_id, RunKind::Run)?,
            };
            let queued = queued_in(tx, &session_id)?;
            // 信箱里已有的信交给这次运行，哪怕它在交给模型之前就最终失败：失败的结清据此了结它们带来的委派，也不会为它们再唤醒
            give_in(tx, &queued.iter().map(|m| m.id).collect::<Vec<_>>(), run_id)?;
            let queued = queued_in(tx, &session_id)?;
            let mut at = position_in(tx, &session_id)?;
            if let Position::Interrupted { calls, after } = at {
                // 被打断的那一步里没出结果的调用不重新执行——副作用可能已经发生，重放一条重启服务的命令会让恢复无限循环
                tracing::warn!(session_id, run_id, tools = ?calls.iter().map(|c| &c.name).collect::<Vec<_>>(),
                    "resuming an interrupted step; its unfinished tool calls are not re-executed");
                let results = calls.iter().map(|c| made_up_result(c, MadeUp::Interrupted)).collect();
                let request = results_request(results).expect("interrupted calls exist");
                history::append_in(tx, &session_id, run_id, &[Message::User(request)])?;
                at = *after;
            }
            let resuming = matches!(at, Position::Resend { .. });
            let adopted = if resuming { adopted_in(tx, &session_id, run_id)? } else { Vec::new() };
            let earlier = consumed_by_in(tx, &session_id, run_id)?;
            let saved = history::saved_by_in(tx, &session_id, run_id)?;
            let last = history::last_message_id_in(tx, &session_id)?;
            let settled = saved.last().is_some_and(|s: &Stored| Some(s.id) == last && !is_stopped(&s.message));
            let agent_name = agent_in(tx, &session.agent_id)?.name;
            let mut audience_mail = earlier;
            audience_mail.extend(adopted.iter().cloned());
            audience_mail.extend(queued.iter().cloned());
            Ok(Begun {
                run_id,
                context: history::load_context(tx, &session_id)?,
                delegated: delegated_in(tx, &session_id)?,
                agent_name,
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
fn adopted_in(c: &Connection, session_id: &str, run_id: i64) -> Result<Vec<Mail>> {
    match history::last_saved_by_in(c, session_id)? {
        Some(tail) if tail != run_id => consumed_by_in(c, session_id, tail),
        _ => Ok(Vec::new()),
    }
}

/// 这次运行开头要交给模型的。
struct Opening {
    /// 开头取走的信件（随第一条新请求记为已处理）
    mail: Vec<i64>,
    paused: Option<Paused>,
    content: Content,
}

async fn opening(core: &Core, begun: &Begun) -> Result<Option<Opening>> {
    let workspace = core.agents.workspace(&begun.session.agent_id);
    Ok(match &begun.at {
        Position::Paused(p) if p.ready => {
            // 回答作为挂起调用的结果交回，其余信件（事件、新消息、迟到的回答）作为内容
            let answering: HashSet<i64> = p.answer_mail.iter().map(|(id, _)| *id).collect();
            let rest: Vec<Mail> = begun.queued.iter().filter(|m| !answering.contains(&m.id)).cloned().collect();
            Some(Opening {
                mail: begun.queued.iter().map(|m| m.id).collect(),
                paused: Some(p.clone()),
                content: content_of(&core.files, &workspace, &rest).await?,
            })
        }
        Position::Fresh => {
            let content = content_of(&core.files, &workspace, &begun.queued).await?;
            (!content.is_empty()).then(|| Opening {
                mail: begun.queued.iter().map(|m| m.id).collect(),
                paused: None,
                content,
            })
        }
        _ => None,
    })
}

/// 之前的尝试已经跑完、只差收尾：从保存的历史与它停在哪得出它怎么结束的。
fn settled_end(begun: &Begun) -> Result<RunEnd> {
    if let Some(p) = begun.at.paused() {
        return Ok(paused_end(&p.pending));
    }
    match begun.context.messages.last() {
        Some(Message::Assistant(r)) => Ok(RunEnd { answer: answer_text(r), stopped: false, waiting_on: Vec::new() }),
        other => Err(broken(format!("a settled run must end with a model response, history ends with {other:?}"))),
    }
}

/// 收尾：记下结局、结清做完的委派、发布 RunEnded（end 为 None 是没事可做的空收尾：只记结局）。
async fn finish(core: &Core, begun: &Begun, end: Option<RunEnd>) -> Result<()> {
    let (run_id, session_id, adopted) = (begun.run_id, begun.session.id.clone(), begun.adopted.clone());
    let (waker, events, live) = (core.waker.clone(), core.events.clone(), core.live.clone());
    let agent_name = begun.agent_name.clone();
    core.db
        .write(move |tx| finish_in(tx, &waker, &events, &live, run_id, &session_id, &agent_name, &adopted, end))
        .await
}

#[allow(clippy::too_many_arguments)]
fn finish_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    events: &Events,
    live: &Live,
    run_id: i64,
    session_id: &str,
    agent_name: &str,
    adopted: &[Mail],
    end: Option<RunEnd>,
) -> Result<()> {
    runs::end_in(tx, live, run_id, Outcome::Done)?;
    let saved = history::saved_by_in(tx, session_id, run_id)?;
    let Some(end) = end.filter(|_| !saved.is_empty()) else { return Ok(()) };
    let session = directory::get_in(tx, session_id)?
        .ok_or_else(|| broken(format!("session {session_id} vanished during its run")))?;
    directory::touch_in(tx, session_id)?;
    let mut handled = consumed_by_in(tx, session_id, run_id)?;
    handled.extend(adopted.iter().cloned());
    // 工作做完了：没有人替它在外面做事，信箱里也没有还要交给模型的信（比如最后一次请求期间才到的结局，它会唤醒下一次运行，
    // 结清由那次运行做）
    let done = !crate::holds::has_active_in(tx, session_id)? && queued_in(tx, session_id)?.is_empty();
    if !end.stopped && end.waiting_on.is_empty() && done {
        let messages: Vec<Message> = saved.into_iter().map(|s| s.message).collect();
        settle_finished_in(tx, waker, &session, &end.answer, &shared_attachments(&messages))?;
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
            answer: end.answer,
            stopped: end.stopped,
            waiting_on: end.waiting_on,
            awaited: handled.iter().any(|m| m.awaited),
        },
    )
}

/// 驱动一次运行的状态（只在这次运行里用）。
struct Driver<'a> {
    core: &'a Core,
    begun: &'a Begun,
    config: &'a AgentDir,
    audience: Audience,
    partial: SharedPartial,
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
    fn session_id(&self) -> &str {
        &self.begun.session.id
    }

    async fn start_run(&self) -> std::result::Result<Arc<Run>, Failure> {
        let run = self
            .core
            .runtime
            .start(RunSpec {
                config: self.config,
                session_id: self.session_id(),
                audience: self.audience,
                origin_note: self.core.notes.of(self.begun.session.origin.origin()),
                summary: self.summary.as_deref(),
            })
            .await?;
        let run = Arc::new(run);
        self.partial.lock().expect("partial lock").run = Some(run.clone());
        Ok(run)
    }

    async fn drive(&mut self, opening: Option<Opening>) -> std::result::Result<RunEnd, Failure> {
        let mut run = self.start_run().await?;
        let mut notices: Vec<String> = run.notices().to_vec();
        let session_id = self.session_id().to_owned();
        let last = self.core.db.read(move |c| history::last_message_id_in(c, &session_id)).await?;
        // 实时展示随这个守卫结束：运行返回、被停止时丢掉、panic 时都一样
        let _live = self.core.live.begin(self.session_id(), last);
        self.steps(&mut run, &mut notices, opening).await
    }

    async fn steps(
        &mut self,
        run: &mut Arc<Run>,
        notices: &mut Vec<String>,
        opening: Option<Opening>,
    ) -> std::result::Result<RunEnd, Failure> {
        // 挂起调用的结局（停在挂起的调用上、能继续时）
        let mut resolved: Option<HashMap<String, Resolution>> = None;
        // 开头的内容与信件：并进这次运行新增的第一条请求
        let mut first: Option<(Content, Vec<i64>)> = None;
        if let Some(o) = opening {
            if let Some(p) = &o.paused {
                resolved = Some(self.hand_over(run, p).await?);
            }
            first = Some((o.content, o.mail));
            if o.paused.is_none() {
                // 新开：开头的内容自己成一条请求
                let (content, mail) = first.take().expect("just set");
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
                Some(Message::Assistant(r)) => {
                    Next::End(RunEnd { answer: answer_text(r), stopped: false, waiting_on: Vec::new() })
                }
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
    /// 中断」）；被问的 agent 挂给用户的文件挂到这次调用的结果上。
    ///
    /// 「交给过运行」单独一个事务提交（Python 与取走信件同一事务）：之后到这一步保存之间失败的，重试时这个批准给「被中断」、
    /// 不执行——偏保守，不会重复执行，窗口只有交出回答到工具开始执行之间。
    async fn hand_over(&self, run: &Run, p: &Paused) -> Result<HashMap<String, Resolution>> {
        let (session_id, answers) = (self.session_id().to_owned(), p.answers.clone());
        let replaced = self.core.db.write(move |tx| crate::approvals::take_in(tx, &session_id, &answers)).await?;
        for (call_id, answer) in &p.answers {
            if let CallResult::AskAgent(a) = answer
                && !a.attachments.is_empty()
            {
                attach(run.scope(), call_id, &a.attachments);
            }
        }
        deferred_results(p, &replaced)
    }

    /// 历史末尾是用户一方的消息：请求模型，保存回复。
    async fn request(&mut self, run: &Run) -> std::result::Result<Next, Failure> {
        self.partial.lock().expect("partial lock").streaming = Some(ResponseBuilder::new());
        let mut on_event = self.on_event();
        let step = run.step(&self.history, &HashMap::new(), &mut on_event).await?;
        let Step::Responded(response) = step else {
            return Err(broken(format!("a request step did not respond: {step:?}")).into());
        };
        self.save(vec![Message::Assistant(response)], Vec::new(), Clear::Streaming).await?;
        Ok(Next::Continue)
    }

    /// 历史末尾是带工具调用的回复：执行工具（或交回挂起调用的结局）。
    async fn tools(
        &mut self,
        run: &Run,
        resolved: Option<HashMap<String, Resolution>>,
        first: Option<(Content, Vec<i64>)>,
        notices: &mut Vec<String>,
    ) -> std::result::Result<Next, Failure> {
        let cut =
            self.history.iter().rposition(|m| matches!(m, Message::Assistant(_))).expect("ends with a response") + 1;
        // 挂起之前已经做完、存在回复之后的结果：连同回答交回 agenter（它们不再执行），新出的结果只存新的那些
        let saved: Vec<ToolResult> = self.history[cut..]
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
            resolved.insert(t.call_id.clone(), Resolution::Result { content: t.content.clone(), is_error: t.is_error });
        }
        let already: HashSet<String> = saved.iter().map(|t| t.call_id.clone()).collect();
        {
            let mut p = self.partial.lock().expect("partial lock");
            p.finished.clear();
            p.finished_at.clear();
        }
        let mut on_event = self.on_event();
        let step = run.step(&self.history[..cut], &resolved, &mut on_event).await?;
        let finished_at = std::mem::take(&mut self.partial.lock().expect("partial lock").finished_at);
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
        match step {
            Step::Results(req) => {
                let results: Vec<ToolResult> = req
                    .parts
                    .into_iter()
                    .filter_map(|p| match p {
                        RequestPart::ToolResult(t) => Some(t),
                        _ => None,
                    })
                    .collect();
                let results = fresh(results);
                {
                    // 整理好的结果换掉事件里的原样：保存之前被停止时停止事务存的就是它们
                    let mut p = self.partial.lock().expect("partial lock");
                    p.finished = results.clone();
                    p.finished_at.clear();
                }
                let mut request = results_request(results).unwrap_or_else(|| Request {
                    parts: Vec::new(),
                    timestamp: Utc::now(),
                    metadata: None,
                });
                let mail = match first {
                    Some((content, mail)) => {
                        append_content(&mut request, content)?;
                        mail
                    }
                    None => Vec::new(),
                };
                self.save_request(request, mail, notices).await?;
                Ok(Next::Continue)
            }
            Step::Paused { results, deferred, approvals } => {
                // 停在挂起的调用上：已做完的结果与这次新请求批准的调用同一个事务记下（分开提交时，进程在两者之间退出会留下
                // 一个没有结果、又不算在等批准的调用）
                let requested: Vec<(String, String)> =
                    approvals.iter().map(|(c, pin)| (c.id.clone(), pin.clone())).collect();
                let pending = paused_calls(deferred, approvals.into_iter().map(|(c, _)| c).collect())?;
                let mut request = results_request(fresh(results));
                // 继续之后又停在挂起的调用上：开头取走的信也在这一步记为已处理，内容随结果存下（下次随它们交给模型）
                let mail = match first {
                    Some((content, mail)) => {
                        let r = request.get_or_insert_with(|| Request {
                            parts: Vec::new(),
                            timestamp: Utc::now(),
                            metadata: None,
                        });
                        append_content(r, content)?;
                        mail
                    }
                    None => Vec::new(),
                };
                let new: Vec<Message> =
                    request.filter(|r| !r.parts.is_empty()).map(Message::User).into_iter().collect();
                let session_id = self.session_id().to_owned();
                self.save_with(new, mail, Clear::Finished, move |tx| {
                    crate::approvals::record_in(tx, &session_id, &requested)
                })
                .await?;
                Ok(Next::End(paused_end(&pending)))
            }
            other => Err(broken(format!("a tools step returned {other:?}")).into()),
        }
    }

    /// 一条新请求（这次运行新增的）：并进新到的信与运行上下文，与它带上的信件同一个事务保存。
    async fn save_request(
        &mut self,
        mut request: Request,
        mut mail: Vec<i64>,
        notices: &mut Vec<String>,
    ) -> Result<()> {
        let session_id = self.session_id().to_owned();
        let queued = self.core.db.read(move |c| queued_in(c, &session_id)).await?;
        let arrived: Vec<Mail> = queued.into_iter().filter(|m| !mail.contains(&m.id)).collect();
        if !arrived.is_empty() {
            let workspace = self.core.agents.workspace(&self.begun.session.agent_id);
            // 运行中途没有挂起的调用（调用只在运行结束时挂起），这时到达的回答都是迟到的
            append_content(&mut request, content_of(&self.core.files, &workspace, &arrived).await?)?;
            mail.extend(arrived.iter().map(|m| m.id));
        }
        let attaching = self.run_context_row.is_none();
        let run_context = if attaching { self.core.runtime.run_context(self.audience) } else { String::new() };
        let context = with_notices(&run_context, notices);
        if !context.is_empty() {
            // 只挂在本次运行新增的第一条请求上（重发的那条已经在库里，不改它）；运行上下文已经挂出去了时，notices 单独挂
            attach_run_context(&mut request, &context)?;
        }
        let ids = self.save(vec![Message::User(request)], mail, Clear::Finished).await?;
        let id = *ids.last().expect("one request saved");
        if attaching {
            self.run_context_row = Some(id);
        }
        notices.clear();
        self.core.live.step(self.session_id(), id);
        Ok(())
    }

    /// 刚保存了一条请求：上下文超过阈值时压缩，从库里重新加载上下文续跑（新的摘要在指令里，所以重新组装这次运行）。
    async fn after_request(&mut self, notices: &mut Vec<String>) -> std::result::Result<Option<Arc<Run>>, Failure> {
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

    async fn compact(&mut self) -> std::result::Result<(), Failure> {
        let compacted = self.core.compactor.compact(self.session_id(), self.config).await?;
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
    async fn save(&mut self, messages: Vec<Message>, mail: Vec<i64>, clear: Clear) -> Result<Vec<i64>> {
        self.save_with(messages, mail, clear, |_| Ok(())).await
    }

    async fn save_with(
        &mut self,
        messages: Vec<Message>,
        mail: Vec<i64>,
        clear: Clear,
        also: impl FnOnce(&mut Tx<'_>) -> Result<()> + Send + 'static,
    ) -> Result<Vec<i64>> {
        // 脱敏只作用于存下的那份；这次运行内存里的历史留明文：下一步执行的就是这些调用，执行时要拿到真的参数（#770）
        let mut stored = messages.clone();
        self.core.redaction.messages(&mut stored);
        let (session_id, run_id, partial) = (self.session_id().to_owned(), self.begun.run_id, self.partial.clone());
        let ids = self
            .core
            .db
            .write(move |tx| {
                let ids = history::append_in(tx, &session_id, run_id, &stored)?;
                consume_in(tx, &mail, run_id)?;
                also(tx)?;
                // 在写线程上、和保存同一个事务里取走已保存的部分：之后的停止事务不会再存一遍
                let mut p = partial.lock().expect("partial lock");
                match clear {
                    Clear::Streaming => p.streaming = None,
                    Clear::Finished => p.finished.clear(),
                }
                tracing::info!(session_id, run_id, messages = ids.len(), consumed_mail = ?mail, "step saved");
                Ok::<_, Error>(ids)
            })
            .await?;
        self.history.extend(messages);
        Ok(ids)
    }

    /// 给界面的实时事件，同时攒下被停止时要保存的部分。
    fn on_event(&self) -> impl FnMut(RunEvent<'_>) + Send + use<> {
        let (live, partial) = (self.core.live.clone(), self.partial.clone());
        let (session_id, agent) = (self.session_id().to_owned(), self.begun.agent_name.clone());
        let renders = self.core.renders.clone();
        move |event| match event {
            RunEvent::Model(e) => {
                if let Some(b) = partial.lock().expect("partial lock").streaming.as_mut() {
                    let _ = b.apply(e);
                }
                match e {
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
                }
            }
            RunEvent::ToolStarted(call) => live.item(&session_id, &agent, call_item(call, Some(Utc::now()), &renders)),
            RunEvent::ToolFinished(result) => {
                let now = Utc::now();
                {
                    let mut p = partial.lock().expect("partial lock");
                    p.finished.push(result.clone());
                    p.finished_at.insert(result.call_id.clone(), now);
                }
                let mut shown = result.clone();
                set_finished_at(&mut shown, now);
                live.item(&session_id, &agent, result_item(&shown, &renders));
            }
        }
    }
}

/// 一步保存时同一个事务里取走的已产生部分。
#[derive(Clone, Copy)]
enum Clear {
    Streaming,
    Finished,
}

/// 被停止时这次尝试已产生、还没保存的部分（存储形态），由停止事务保存在最前面；还没建好运行记录的尝试没有。
pub(crate) async fn unsaved_of(slot: &PartialSlot) -> Result<Option<Unsaved>> {
    let Some(partial) = slot.lock().expect("partial slot lock").clone() else { return Ok(None) };
    let (run_id, streaming, finished, finished_at, run) = {
        let mut p = partial.lock().expect("partial lock");
        (
            p.run_id,
            p.streaming.take(),
            std::mem::take(&mut p.finished),
            std::mem::take(&mut p.finished_at),
            p.run.clone(),
        )
    };
    let mut messages = Vec::new();
    if let Some(b) = streaming {
        let parts = b.into_parts();
        if !parts.is_empty() {
            messages.push(Message::Assistant(interrupted_response(parts, "", "")));
        }
    }
    if !finished.is_empty() {
        let run = run.expect("tool results come from a started run");
        let mut results = Vec::new();
        for r in finished {
            let mut r = run.settled(r).await?;
            if let Some(at) = finished_at.get(&r.call_id) {
                set_finished_at(&mut r, *at);
            }
            results.push(r);
        }
        messages.extend(results_request(results).map(Message::User));
    }
    Ok(Some(Unsaved { run_id, messages }))
}
