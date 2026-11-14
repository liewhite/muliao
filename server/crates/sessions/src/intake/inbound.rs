//! 来信：一切投进会话信箱的东西的唯一入口（docs/architecture.md 第 14 节）。
//!
//! - 用户在界面上的消息（[`UserMessage`]，[`crate::service::SessionService::receive`]）、委派来的任务（会话领域内部）：
//!   投不进报领域错误；别的领域（触发器、issue）用 [`SenderInbox::receive_in`] 在自己的事务里投信（同一套规则 [`check_in`]），
//!   得到「投进去了」（[`Accepted`]）或「投不进 + 原因」（[`Refused`]），由发送方逐种决定反应。
//! - 目标会话三种：续写已有的、新建、按发送方定位（同一发送方给同一个 agent 的来信进同一个会话，没有就建）。
//! - 续写规则：用户可以在任何会话里继续；其他来源只能续写自己发起的会话。
//! - 新建会话只有 [`create_session_in`] 一处实现（来信与 thread 都经过它）。会话只为能运行的 agent 新建、继续（在本事务里
//!   检查 agent 没在删除、没归档）。

use muliao_platform::ids::{AgentId, CallId, DelegationId, FileId, SessionId, new_id};

use muliao_assets::agents::{Availability, availability_in, runnable_agent_in};
use muliao_files::load_metas;
use muliao_platform::db::Tx;
use rusqlite::Connection;

use crate::directory::{self, NewSession, Session, keyed_in};
use crate::execution::actor::Waker;
use crate::intake::origins::{Origin, Sender, SessionOrigin, ThreadName};
use crate::mailbox::{MailBody, NewMail, post_in, queued_in};
use crate::waiting::answers::{CallResult, Choice, Decision, QuestionAnswer, answer_result};
use crate::waiting::pending::open_asks_of;
use crate::waiting::pending::position_in;
use muliao_platform::errors::{Error, Result, conflict, forbidden, invalid, not_found};

const TITLE_MAX: usize = 60;
/// 会话标题的长度上限：发送方给的标题过长时在新建会话时截断
const TITLE_LENGTH: usize = 200;

/// 一封来信的具体发送方。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) enum Party {
    User,
    /// 委派来的信：发送方是父会话
    Parent(SessionId),
    Sender(Sender),
}

impl Party {
    fn origin(&self) -> Origin {
        match self {
            Party::User => Origin::User,
            Party::Parent(_) => Origin::Delegation,
            Party::Sender(s) => s.origin(),
        }
    }
}

/// 别的领域的来信要投到的会话。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SenderTarget {
    /// 新建一个顶层会话，标题由发送方给出（触发器）
    New { agent_id: AgentId, title: String },
    /// 按发送方定位：这个发送方给这个 agent 的最新一个会话，没有就以 title 新建（issue：每个 issue 与 agent 对应唯一一个会话）
    Keyed { agent_id: AgentId, title: String },
}

/// 别的领域（触发器、issue）的来信：只有文字，没有人在等（处理它的运行不推送）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SenderMail {
    pub sender: Sender,
    pub content: String,
    pub session: SenderTarget,
}

/// 用户在界面上发的消息要投到的会话。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UserTarget {
    Existing(SessionId),
    /// 新建会话：标题由这条消息推导（文字的第一行，只发了文件时用文件名）
    NewChat {
        agent_id: AgentId,
    },
}

/// 用户在界面上发来的一件事：一条消息（续写或新建会话，可以带附件）、对一次挂起提问的回答，或对一次等批准调用的决定
/// （后两种只对已有会话）；用户在等它的回答。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UserMessage {
    Message { session: UserTarget, content: String, attachments: Vec<FileId> },
    Answer { session_id: SessionId, call_id: CallId, answers: Vec<QuestionAnswer> },
    Approval { session_id: SessionId, call_id: CallId, choice: Choice },
}

/// 别的领域的来信投不进的原因：目标 agent 收不了信（只有收信入口判断，发送方按原因决定自己怎么反应）。发送方只投新会话
/// 或按自己定位的会话，所以没有「会话不在了」「不准续写」这两种（那是续写已有会话的用户、父会话才会碰到的，直接报错）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// 目标 agent 已归档：取消归档后又能投
    Archived,
    /// 目标 agent 不存在、正在删除
    Gone,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub reason: Refusal,
    pub detail: String,
}

impl Refused {
    fn into_error(self) -> Error {
        match self.reason {
            Refusal::Archived => conflict(self.detail),
            Refusal::Gone => not_found(self.detail),
        }
    }
}

/// 投进去了：信进的会话。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Accepted {
    pub session_id: SessionId,
}

/// 一封来信的内部形式，按种类各带自己的字段。
pub(crate) enum Inbound {
    /// 用户的一条消息（回答与批准不是来信，走 [`reply_in`]）：用户在等它的回答
    User { session: UserTarget, content: String, attachments: Vec<FileId> },
    /// 别的领域（触发器、issue）的来信：没有人在等
    Sender(SenderMail),
    /// 委派来的信：发送方是父会话 parent，投给它已开出的 thread
    Delegated { parent: SessionId, thread: SessionId, content: String, handback: Handback },
}

/// 委派的 thread 的结局交给谁。
pub(crate) enum Handback {
    /// 用户在等 thread 的回答（普通 delegate_task）
    User,
    /// 交回发起方：是哪项委派（thread_calls 的行）
    Parent(DelegationId),
}

/// 用户对已有会话里一次挂起调用的回复（[`reply_in`]）。
pub(crate) enum CallReply {
    /// 提问的每题回答
    Answer(Vec<QuestionAnswer>),
    /// 等批准的调用的决定
    Approval(Choice),
}

impl Inbound {
    fn from(&self) -> Party {
        match self {
            Inbound::User { .. } => Party::User,
            Inbound::Sender(m) => Party::Sender(m.sender),
            Inbound::Delegated { parent, .. } => Party::Parent(parent.clone()),
        }
    }

    fn content(&self) -> &str {
        match self {
            Inbound::User { content, .. } | Inbound::Delegated { content, .. } => content,
            Inbound::Sender(m) => &m.content,
        }
    }

    fn attachments(&self) -> &[FileId] {
        match self {
            Inbound::User { attachments, .. } => attachments,
            Inbound::Sender(_) | Inbound::Delegated { .. } => &[],
        }
    }

    /// 投成信件：(正文, 附件, 委派来的是哪项委派, 用户在不在等)
    fn into_mail(self) -> (String, Vec<FileId>, Option<DelegationId>, bool) {
        match self {
            Inbound::User { content, attachments, .. } => (content, attachments, None, true),
            Inbound::Sender(m) => (m.content, Vec::new(), None, false),
            Inbound::Delegated { content, handback: Handback::User, .. } => (content, Vec::new(), None, true),
            Inbound::Delegated { content, handback: Handback::Parent(d), .. } => (content, Vec::new(), Some(d), false),
        }
    }
}

fn shorten(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        return text.to_owned();
    }
    let mut s: String = text.chars().take(limit - 1).collect();
    s.push('…');
    s
}

/// 没给标题的新会话的标题：内容的第一行，过长时截断。
pub(super) fn default_title(text: &str) -> String {
    shorten(text.trim().lines().next().unwrap_or(""), TITLE_MAX)
}

/// 会话的发起方。委派会话的发起方是它的父会话（父会话删除后谁都不是：只有用户能续写）。
fn sender_of(session: &Session) -> Option<Party> {
    match &session.origin {
        SessionOrigin::User => Some(Party::User),
        SessionOrigin::Delegation { parent_session_id, .. } => parent_session_id.clone().map(Party::Parent),
        SessionOrigin::Trigger { .. } | SessionOrigin::Issue { .. } => session.origin.sender().map(Party::Sender),
    }
}

fn may_continue(session: &Session, from: &Party) -> bool {
    from.origin().writes_anywhere() || sender_of(session).as_ref() == Some(from)
}

/// 谁开出新会话：用户、别的领域的发送方，或父会话开 thread（只有委派开 thread）。
pub(crate) enum Opener<'a> {
    User,
    Sender(Sender),
    Thread { parent: &'a SessionId, name: &'a ThreadName },
}

/// 新建会话的唯一实现（来信与开 thread 都经过这里）：只为能运行的 agent 建。
pub(crate) fn create_session_in(c: &Tx<'_>, opener: Opener<'_>, agent_id: &AgentId, title: &str) -> Result<Session> {
    runnable_agent_in(c, agent_id)?;
    let origin = match opener {
        Opener::User => SessionOrigin::User,
        Opener::Sender(s) => SessionOrigin::from(s),
        Opener::Thread { parent, name } => {
            SessionOrigin::Delegation { thread: name.clone(), parent_session_id: Some(parent.to_owned()) }
        }
    };
    let id = SessionId::new(new_id());
    directory::add_in(c, NewSession { id: &id, agent_id, title: &shorten(title, TITLE_LENGTH), origin })
}

/// 新来信能不能交给这个 agent（不能时是原因）。「agent 能不能收信」只在这里回答：发送方要在投信前判断（如触发器不为
/// 收不了信的 agent 跑前置检查）也用它（[`SenderInbox::refusal_in`]）。
fn agent_refusal_in(c: &Connection, agent_id: &AgentId) -> Result<Option<Refused>> {
    Ok(match availability_in(c, agent_id)? {
        Availability::Runnable(_) => None,
        Availability::Archived(agent) => Some(Refused {
            reason: Refusal::Archived,
            detail: format!("agent {:?} is archived; unarchive it first", agent.name),
        }),
        Availability::Gone => Some(Refused { reason: Refusal::Gone, detail: format!("agent {agent_id:?} not found") }),
    })
}

/// 校验通过的来信要进的会话：续写已有的，或新建一个（谁开、给哪个 agent、标题）。
pub(crate) enum Checked {
    Existing(Session),
    New { opener: Opener<'static>, agent_id: AgentId, title: String },
}

impl Checked {
    fn agent_id(&self) -> &AgentId {
        match self {
            Checked::Existing(s) => &s.agent_id,
            Checked::New { agent_id, .. } => agent_id,
        }
    }
}

/// 续写已有的会话：会话要在、发送方要准续写。
fn existing_in(c: &Connection, id: &SessionId, from: &Party) -> Result<Session> {
    let session = directory::get_in(c, id)?.ok_or_else(|| not_found(format!("session {id:?} not found")))?;
    if !may_continue(&session, from) {
        tracing::warn!(session_id = %session.id, sender = ?from, "continuation refused");
        return Err(forbidden(format!(
            "session {} was not started by this sender; it may only continue its own sessions",
            session.id
        )));
    }
    Ok(session)
}

/// 来信的校验规则（只读）：agent 收不了信的是 Refused。续写已有会话时（只有用户与父会话会）会话不在了、发送方不准续写
/// 直接报领域错误；内容为空、附件不存在是发送方自己的错（只有用户的消息会碰到），也报错。
pub(crate) fn check_in(c: &Connection, inbound: &Inbound) -> Result<std::result::Result<Checked, Refused>> {
    if inbound.content().trim().is_empty() && inbound.attachments().is_empty() {
        return Err(invalid("message must not be empty"));
    }
    let attachments = load_metas(c, inbound.attachments())?;
    let checked = match inbound {
        Inbound::User { session: UserTarget::Existing(id), .. } => Checked::Existing(existing_in(c, id, &Party::User)?),
        Inbound::Delegated { parent, thread, .. } => {
            Checked::Existing(existing_in(c, thread, &Party::Parent(parent.clone()))?)
        }
        Inbound::User { session: UserTarget::NewChat { agent_id }, content, .. } => {
            // 没给标题：取文字的第一行，只发了文件时用文件名
            let title = if content.trim().is_empty() {
                default_title(&attachments.iter().map(|m| m.name.as_str()).collect::<Vec<_>>().join(", "))
            } else {
                default_title(content)
            };
            Checked::New { opener: Opener::User, agent_id: agent_id.clone(), title }
        }
        Inbound::Sender(SenderMail { sender, session: SenderTarget::New { agent_id, title }, .. }) => {
            Checked::New { opener: Opener::Sender(*sender), agent_id: agent_id.clone(), title: title.clone() }
        }
        Inbound::Sender(SenderMail { sender, session: SenderTarget::Keyed { agent_id, title }, .. }) => {
            match keyed_in(c, *sender, agent_id)? {
                Some(s) => Checked::Existing(s),
                None => {
                    Checked::New { opener: Opener::Sender(*sender), agent_id: agent_id.clone(), title: title.clone() }
                }
            }
        }
    };
    Ok(match agent_refusal_in(c, checked.agent_id())? {
        Some(r) => Err(r),
        None => Ok(checked),
    })
}

/// 用户对一次挂起调用的回复：要对应还在等的提问或等批准的调用。返回回答信件里存的结果。
fn resolve_reply(c: &Connection, session_id: &SessionId, call_id: &CallId, reply: &CallReply) -> Result<CallResult> {
    let asks = open_asks_of(&position_in(c, session_id)?, &queued_in(c, session_id)?);
    match reply {
        CallReply::Answer(given) => {
            let questions = asks.question(call_id).ok_or_else(|| {
                conflict(format!("question {call_id:?} is not waiting for an answer in session {session_id}"))
            })?;
            Ok(CallResult::AskUser(answer_result(questions, given)?))
        }
        CallReply::Approval(choice) => {
            if !asks.can_approve(call_id) {
                return Err(conflict(format!("call {call_id:?} is not waiting for approval in session {session_id}")));
            }
            Ok(CallResult::Approval(Decision { choice: *choice }))
        }
    }
}

/// 所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。投不进按原因报领域错误（调用方在等结果的来信用）。
pub(crate) fn receive_in(tx: &mut Tx<'_>, waker: &Waker, inbound: Inbound) -> Result<Session> {
    let checked = check_in(tx, &inbound)?.map_err(Refused::into_error)?;
    receive_into(tx, waker, inbound, checked)
}

fn receive_into(tx: &mut Tx<'_>, waker: &Waker, inbound: Inbound, checked: Checked) -> Result<Session> {
    let session = match checked {
        Checked::Existing(s) => s,
        Checked::New { opener, agent_id, title } => create_session_in(tx, opener, &agent_id, &title)?,
    };
    directory::touch_in(tx, &session.id)?;
    let from = inbound.from();
    let (content, attachments, delegation_id, awaited) = inbound.into_mail();
    let body = MailBody::Message { origin: from.origin(), content, attachments, delegation_id };
    post_in(tx, waker, &session.id, NewMail { body, awaited })?;
    tracing::info!(session_id = %session.id, sender = ?from, "message received");
    Ok(session)
}

/// 用户对已有会话里一次挂起调用的回复（回答提问、批准调用）：会话要在、agent 要能收信，调用要还在等。结果作为回答信件
/// 投进信箱，用户在等。
pub(crate) fn reply_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    session_id: &SessionId,
    call_id: CallId,
    reply: CallReply,
) -> Result<Session> {
    let session =
        directory::get_in(tx, session_id)?.ok_or_else(|| not_found(format!("session {session_id:?} not found")))?;
    if let Some(refused) = agent_refusal_in(tx, &session.agent_id)? {
        return Err(refused.into_error());
    }
    directory::touch_in(tx, &session.id)?;
    let result = resolve_reply(tx, &session.id, &call_id, &reply)?;
    post_in(tx, waker, &session.id, NewMail::answer(call_id, result, true))?;
    tracing::info!(session_id = %session.id, "reply received");
    Ok(session)
}

/// 别的领域（触发器、issue）投信的唯一入口，在发送方的事务里：发送方记下的状态与投信一起提交。
#[derive(Clone)]
pub struct SenderInbox {
    waker: Waker,
}

impl SenderInbox {
    pub fn new(waker: Waker) -> Self {
        Self { waker }
    }

    /// 这个 agent 现在收不收得了新来信（只读）：发送方在投信前要先知道时用，投信本身仍以 [`Self::receive_in`] 的结果为准。
    pub fn refusal_in(c: &Connection, agent_id: &AgentId) -> Result<Option<Refused>> {
        agent_refusal_in(c, agent_id)
    }

    /// 投不进的返回 Refused（原因由这里判断，发送方只决定自己怎么反应）。
    pub fn receive_in(&self, tx: &mut Tx<'_>, mail: SenderMail) -> Result<std::result::Result<Accepted, Refused>> {
        let inbound = Inbound::Sender(mail);
        Ok(match check_in(tx, &inbound)? {
            Err(refused) => Err(refused),
            Ok(checked) => Ok(Accepted { session_id: receive_into(tx, &self.waker, inbound, checked)?.id }),
        })
    }
}
