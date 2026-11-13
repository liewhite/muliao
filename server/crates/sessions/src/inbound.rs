//! 来信：一切投进会话信箱的东西的唯一入口（docs/architecture.md 第 14 节）。
//!
//! - 用户在界面上的消息（[`UserMessage`]，[`crate::service::SessionService::receive`]）、委派来的任务（会话领域内部）：
//!   投不进报领域错误；别的领域（触发器、issue）用 [`SenderInbox::receive_in`] 在自己的事务里投信（同一套规则 [`check_in`]），
//!   得到「投进去了」（[`Accepted`]）或「投不进 + 原因」（[`Refused`]），由发送方逐种决定反应。
//! - 目标会话三种：续写已有的、新建、按发送方定位（同一发送方给同一个 agent 的来信进同一个会话，没有就建）。
//! - 续写规则：用户可以在任何会话里继续；其他来源只能续写自己发起的会话。
//! - 新建会话只有 [`create_session_in`] 一处实现（来信与 thread 都经过它）。会话只为能运行的 agent 新建、继续（在本事务里
//!   检查 agent 没在删除、没归档）。

use muliao_platform::ids::{AgentId, SessionId, new_id};
use std::collections::BTreeMap;

use muliao_assets::agents::{Availability, agent_in, availability_in, runnable_agent_in};
use muliao_files::{FileMeta, load_metas};
use muliao_platform::db::Tx;
use rusqlite::Connection;

use crate::actor::Waker;
use crate::answers::{CallResult, Choice, Decision, QuestionAnswer, answer_result};
use crate::directory::{self, NewSession, Session, keyed_among};
use crate::mailbox::{MailBody, NewMail, post_in, queued_in};
use crate::origins::{Origin, Sender, SessionOrigin};
use crate::pending::open_asks_of;
use crate::work::position_in;
use muliao_platform::errors::{Error, Result, conflict, forbidden, invalid, not_found};

const TITLE_MAX: usize = 60;
/// 会话标题的长度上限：发送方给的标题过长时在新建会话时截断
const TITLE_LENGTH: usize = 200;

/// 一封来信的具体发送方。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Party {
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
    Message { session: UserTarget, content: String, attachments: Vec<String> },
    Answer { session_id: SessionId, call_id: String, answers: Vec<QuestionAnswer> },
    Approval { session_id: SessionId, call_id: String, choice: Choice },
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

/// 一封来信的内部形式。
pub(crate) struct Inbound {
    pub from: Party,
    pub body: Body,
    pub target: Target,
    /// 用户在等它的回答
    pub awaited: bool,
    /// 委派来的信件：是哪项委派（thread_calls 的行）
    pub delegation: Option<i64>,
}

/// 来信的内容：文字与附件，或（只有用户会发的）对一次挂起调用的回复。
pub(crate) enum Body {
    Message { content: String, attachments: Vec<String> },
    Reply { call_id: String, reply: CallReply },
}

/// 用户对一次挂起调用的回复。
pub(crate) enum CallReply {
    /// 提问的每题回答
    Answer(Vec<QuestionAnswer>),
    /// 等批准的调用的决定
    Approval(Choice),
}

pub(crate) enum Target {
    Existing(SessionId),
    New { agent_id: AgentId, title: Option<String> },
    Keyed { agent_id: AgentId, title: String },
}

impl Inbound {
    pub(crate) fn user(m: UserMessage) -> Self {
        let (target, body) = match m {
            UserMessage::Message { session, content, attachments } => {
                let target = match session {
                    UserTarget::Existing(id) => Target::Existing(id),
                    UserTarget::NewChat { agent_id } => Target::New { agent_id, title: None },
                };
                (target, Body::Message { content, attachments })
            }
            UserMessage::Answer { session_id, call_id, answers } => {
                (Target::Existing(session_id), Body::Reply { call_id, reply: CallReply::Answer(answers) })
            }
            UserMessage::Approval { session_id, call_id, choice } => {
                (Target::Existing(session_id), Body::Reply { call_id, reply: CallReply::Approval(choice) })
            }
        };
        Inbound { from: Party::User, body, target, awaited: true, delegation: None }
    }

    fn sender(m: SenderMail) -> Self {
        Inbound {
            from: Party::Sender(m.sender),
            body: Body::Message { content: m.content, attachments: Vec::new() },
            target: match m.session {
                SenderTarget::New { agent_id, title } => Target::New { agent_id, title: Some(title) },
                SenderTarget::Keyed { agent_id, title } => Target::Keyed { agent_id, title },
            },
            awaited: false,
            delegation: None,
        }
    }

    /// 委派来的信：发送方是父会话。awaited：用户是否在等 thread 的回答（普通 delegate_task 是，要交回结局的不是）。
    pub(crate) fn delegated(
        parent_id: &SessionId,
        thread_id: &SessionId,
        content: &str,
        awaited: bool,
        delegation: Option<i64>,
    ) -> Self {
        Inbound {
            from: Party::Parent(parent_id.to_owned()),
            body: Body::Message { content: content.to_owned(), attachments: Vec::new() },
            target: Target::Existing(thread_id.to_owned()),
            awaited,
            delegation,
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
pub(crate) fn default_title(text: &str) -> String {
    shorten(text.trim().lines().next().unwrap_or(""), TITLE_MAX)
}

/// 会话的发起方。委派会话的发起方是它的父会话（父会话删除后谁都不是：只有用户能续写）。
fn sender_of(session: &Session) -> Option<Party> {
    match session.origin {
        SessionOrigin::User => Some(Party::User),
        SessionOrigin::Delegation { .. } => session.parent_session_id.clone().map(Party::Parent),
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
    Thread { parent: &'a SessionId, name: &'a str },
}

/// 新建会话的唯一实现（来信与开 thread 都经过这里）：只为能运行的 agent 建。
pub(crate) fn create_session_in(
    c: &Connection,
    opener: Opener<'_>,
    agent_id: &AgentId,
    title: &str,
) -> Result<Session> {
    runnable_agent_in(c, agent_id)?;
    let (origin, parent_session_id) = match opener {
        Opener::User => (SessionOrigin::User, None),
        Opener::Sender(s) => (SessionOrigin::from(s), None),
        Opener::Thread { parent, name } => (SessionOrigin::Delegation { thread: name.to_owned() }, Some(parent)),
    };
    let id = SessionId::new(new_id());
    directory::add_in(
        c,
        NewSession { id: &id, agent_id, title: &shorten(title, TITLE_LENGTH), origin, parent_session_id },
    )
}

/// 新来信能不能交给这个 agent（不能时是原因）。「agent 能不能收信」只在这里回答：发送方要在投信前判断（如触发器不为
/// 收不了信的 agent 跑前置检查）也用它（[`SenderInbox::refusal_in`]）。
fn agent_refusal_in(c: &Connection, agent_id: &AgentId) -> Result<Option<Refused>> {
    Ok(match availability_in(c, agent_id)? {
        Availability::Runnable => None,
        Availability::Archived => {
            let name = agent_in(c, agent_id)?.name;
            Some(Refused {
                reason: Refusal::Archived,
                detail: format!("agent {name:?} is archived; unarchive it first"),
            })
        }
        Availability::Gone => Some(Refused { reason: Refusal::Gone, detail: format!("agent {agent_id:?} not found") }),
    })
}

/// 校验通过的来信：要续写的会话（新建时为 None）与附件的元数据。
pub(crate) struct Checked {
    session: Option<Session>,
    attachments: Vec<FileMeta>,
}

/// 来信的校验规则（只读）：agent 收不了信的是 Refused。续写已有会话时（只有用户与父会话会）会话不在了、发送方不准续写
/// 直接报领域错误；内容为空、附件不存在是发送方自己的错（只有用户的消息会碰到），也报错。
pub(crate) fn check_in(c: &Connection, inbound: &Inbound) -> Result<std::result::Result<Checked, Refused>> {
    let attachments = match &inbound.body {
        Body::Message { content, attachments } if content.trim().is_empty() && attachments.is_empty() => {
            return Err(invalid("message must not be empty"));
        }
        Body::Message { attachments, .. } => load_metas(c, attachments)?,
        Body::Reply { .. } => Vec::new(),
    };
    let session = match &inbound.target {
        Target::New { agent_id, .. } => {
            return Ok(match agent_refusal_in(c, agent_id)? {
                Some(r) => Err(r),
                None => Ok(Checked { session: None, attachments }),
            });
        }
        Target::Keyed { agent_id, .. } => {
            let Party::Sender(sender) = &inbound.from else {
                panic!("only a sender with an identity can address a session by key")
            };
            match keyed_among(c, &[*sender], Some(agent_id))?.remove(&(*sender, agent_id.clone())) {
                Some(s) => s,
                None => {
                    return Ok(match agent_refusal_in(c, agent_id)? {
                        Some(r) => Err(r),
                        None => Ok(Checked { session: None, attachments }),
                    });
                }
            }
        }
        Target::Existing(id) => {
            let session = directory::get_in(c, id)?.ok_or_else(|| not_found(format!("session {id:?} not found")))?;
            if !may_continue(&session, &inbound.from) {
                tracing::warn!(session_id = %session.id, sender = ?inbound.from, "continuation refused");
                return Err(forbidden(format!(
                    "session {} was not started by this sender; it may only continue its own sessions",
                    session.id
                )));
            }
            session
        }
    };
    Ok(match agent_refusal_in(c, &session.agent_id)? {
        Some(r) => Err(r),
        None => Ok(Checked { session: Some(session), attachments }),
    })
}

/// 用户对一次挂起调用的回复：要对应还在等的提问或等批准的调用。返回回答信件里存的结果。
fn resolve_reply(c: &Connection, session_id: &SessionId, call_id: &str, reply: &CallReply) -> Result<CallResult> {
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
            let pin = crate::approvals::pin_in(c, session_id, call_id)?;
            Ok(CallResult::Approval(Decision { choice: *choice, pin }))
        }
    }
}

/// 所有来信的唯一入口：确定会话（继续或新建），把它投进会话的信箱。投不进按原因报领域错误（调用方在等结果的来信用）。
pub(crate) fn receive_in(tx: &mut Tx<'_>, waker: &Waker, inbound: Inbound) -> Result<Session> {
    let checked = check_in(tx, &inbound)?.map_err(Refused::into_error)?;
    receive_into(tx, waker, inbound, checked)
}

fn receive_into(tx: &mut Tx<'_>, waker: &Waker, inbound: Inbound, checked: Checked) -> Result<Session> {
    let session = match checked.session {
        Some(s) => s,
        None => {
            let opener = match &inbound.from {
                Party::User => Opener::User,
                Party::Sender(s) => Opener::Sender(*s),
                // 委派来的信只投给已开出的 thread（Target::Existing）
                Party::Parent(_) => panic!("delegated mail resolved to no thread"),
            };
            let (agent_id, title) = match &inbound.target {
                Target::Existing(id) => panic!("inbound for session {id:?} resolved to no session"),
                Target::New { agent_id, title: Some(t) } | Target::Keyed { agent_id, title: t } => {
                    (agent_id, t.clone())
                }
                Target::New { agent_id, title: None } => {
                    let title = match &inbound.body {
                        Body::Message { content, .. } if !content.trim().is_empty() => default_title(content),
                        // 只发了文件：用文件名（回复只投给已有会话，走不到这里）
                        Body::Message { .. } | Body::Reply { .. } => default_title(
                            &checked.attachments.iter().map(|m| m.name.as_str()).collect::<Vec<_>>().join(", "),
                        ),
                    };
                    (agent_id, title)
                }
            };
            create_session_in(tx, opener, agent_id, &title)?
        }
    };
    directory::touch_in(tx, &session.id)?;
    let origin = inbound.from.origin();
    let mail = match inbound.body {
        Body::Message { content, attachments } => NewMail {
            body: MailBody::Message { origin, content, attachments, delegation_id: inbound.delegation },
            awaited: inbound.awaited,
        },
        Body::Reply { call_id, reply } => {
            let result = resolve_reply(tx, &session.id, &call_id, &reply)?;
            NewMail::answer(BTreeMap::from([(call_id, result)]), origin == Origin::User, inbound.awaited)
        }
    };
    post_in(tx, waker, &session.id, mail)?;
    tracing::info!(session_id = %session.id, sender = ?inbound.from, "message received");
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
        let inbound = Inbound::sender(mail);
        Ok(match check_in(tx, &inbound)? {
            Err(refused) => Err(refused),
            Ok(checked) => Ok(Accepted { session_id: receive_into(tx, &self.waker, inbound, checked)?.id }),
        })
    }
}
