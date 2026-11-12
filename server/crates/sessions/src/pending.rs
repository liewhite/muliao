//! 挂起的工具调用：模型调用了一个要等别人回信的工具（ask_user 等用户、ask_agent 等另一个 agent），或工具执行前要用户
//! 批准。运行停在这里正常结束；回信是一封回答信件，作为那次调用的结果交回，模型看到的就是「我问了，对方这样答」。
//! 设计见 docs/architecture.md「挂起的调用」。
//!
//! - 三种的差别只写在 [`Waiting`] 的几个方法里（要不要占用才算挂起、没交出去时能否原样重放、停止时是否仍挂着），各处查它，
//!   不按工具名分支。ask_user、ask_agent 按工具名认；等批准的按记录认（同一个工具多数调用不用批准）。一次调用是哪一种只经
//!   [`kind_of`] 认。
//! - 会话停在哪（[`position`]，纯函数，唯一的判断）：空闲、新开、从历史续跑、被打断、等回答。由历史、交出去的永久事实
//!   （[`HandedOut`]）与信箱推导，不另存状态；actor、唤醒、停止、等用户的调用、交回结局都 match 它。
//! - 什么时候继续（[`Paused::ready`]）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件留在信箱里等它们
//!   一起交给模型。

use std::collections::{HashMap, HashSet};

use agenter::{Message, Resolution, ToolCall, ToolResult};

use crate::answers::{CallResult, Question, Waiting, questions_of, skipped};
use crate::conventions::{is_stopped, unanswered_calls};
use crate::error::{Result, broken};
use crate::mailbox::{Mail, MailKind};

pub const ASK_USER: &str = "ask_user";
pub const ASK_AGENT: &str = "ask_agent";

impl Waiting {
    /// 交出去时留下带调用 id 的占用，有占用才算挂起；没有占用的调用是工具还没执行完。
    fn needs_hold(self) -> bool {
        self == Waiting::AskAgent
    }

    /// 没有挂起、也没有结果的调用可以原样重新执行（按调用 id 幂等，没有别的副作用），续跑时不补「被中断」。
    fn replayable(self) -> bool {
        self == Waiting::AskAgent
    }

    /// 用户停止时不补「已停止」，仍挂着等回答。
    fn kept_open_on_stop(self) -> bool {
        self == Waiting::AskUser
    }
}

/// 这次调用若挂起，是哪一种：记过等批准的是等批准，否则按工具名认。提问的参数不合法（没有问题）时为 None：这样的
/// 调用没执行过，不是在等用户回答，停止与续跑都按普通调用补结果。
pub fn kind_of(call: &ToolCall, approvals: &HashSet<String>) -> Option<Waiting> {
    if approvals.contains(&call.id) {
        return Some(Waiting::Approval);
    }
    match call.name.as_str() {
        ASK_USER if !questions_of(&call.args).is_empty() => Some(Waiting::AskUser),
        ASK_AGENT => Some(Waiting::AskAgent),
        _ => None,
    }
}

/// 挂起调用的永久事实（其余由历史推导）：已经交出去的 ask_agent 调用（带 call_id 的占用），与停下来等批准的调用。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct HandedOut {
    pub delegated: HashSet<String>,
    pub approvals: HashSet<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct PendingCall {
    pub call: ToolCall,
    pub kind: Waiting,
}

/// 等回答：停在挂起的调用上。
#[derive(Debug, Clone, PartialEq)]
pub struct Paused {
    /// 挂起的调用，按出现顺序
    pub pending: Vec<PendingCall>,
    /// 信箱里对它们的回答
    pub answers: HashMap<String, CallResult>,
    /// 带着这些回答的信件：信件 id 与它回答的调用
    pub answer_mail: Vec<(i64, Vec<String>)>,
    /// 现在能不能继续
    pub ready: bool,
    /// 同一条回复里没出结果、不挂起、能原样重新执行的调用（还没交出去的 ask_agent）：继续时执行，停止时补「已停止」
    pub replays: Vec<ToolCall>,
}

impl Paused {
    pub fn get(&self, call_id: &str) -> Option<&PendingCall> {
        self.pending.iter().find(|p| p.call.id == call_id)
    }
}

/// 会话现在停在哪。
#[derive(Debug, Clone, PartialEq)]
pub enum Position {
    /// 没有挂起的调用、没有要续跑的，信箱也空了
    Idle,
    /// 没有挂起的调用、没有要续跑的，信箱里有信：作为下一次运行的开头交给模型
    Fresh,
    /// 从历史续跑：历史停在一条没得到回复、没标停止的请求上（重发它，replays 为空），或最后的回复里没出结果的调用都能原样
    /// 重新执行
    Resend {
        replays: Vec<ToolCall>,
    },
    Paused(Paused),
    /// 被打断：最后的回复里有没出结果、也不在挂起的调用（执行工具时被打断）。它们不重新执行，先补上「被中断」的结果再看
    /// after（只会是 Resend 或 Paused）。只要有一个不能重放，整步都补
    Interrupted {
        calls: Vec<ToolCall>,
        after: Box<Position>,
    },
}

impl Position {
    /// 挂着的调用（被打断时是补完结果之后仍挂着的）。
    pub fn paused(&self) -> Option<&Paused> {
        match self {
            Position::Paused(p) => Some(p),
            Position::Interrupted { after, .. } => after.paused(),
            _ => None,
        }
    }

    /// 停在挂起的调用上、还不能继续：等回答或新消息，事件等它们一起交给模型（唤醒据此不跑）。
    pub fn waiting_for_answers(&self) -> bool {
        self.paused().is_some_and(|p| !p.ready)
    }
}

/// 会话现在停在哪（纯函数）。history 至少要含最后一条回复起的那一段；handed 是交出去的永久事实；queued 是信箱里排着的
/// 信件。
pub fn position(history: &[Message], handed: &HandedOut, queued: &[Mail]) -> Result<Position> {
    let unanswered = unanswered_calls(history)?;
    let mut pending: Vec<PendingCall> = Vec::new();
    let mut others: Vec<ToolCall> = Vec::new();
    for c in unanswered {
        match kind_of(&c, &handed.approvals) {
            Some(kind) if !kind.needs_hold() || handed.delegated.contains(&c.id) => {
                pending.push(PendingCall { call: c, kind })
            }
            _ => others.push(c),
        }
    }
    let last = history.last();
    let on_response = matches!(last, Some(Message::Assistant(_)));
    let replayable = |c: &ToolCall| kind_of(c, &handed.approvals).is_some_and(Waiting::replayable);
    // 只要有一个不能重放，整步都补「被中断」；全都能重放、历史又停在这条回复上时原样重新执行
    let replays: Vec<ToolCall> = if on_response && others.iter().all(replayable) { others.clone() } else { Vec::new() };
    let interrupted: Vec<ToolCall> = if replays.is_empty() { others } else { Vec::new() };
    let waiting = if pending.is_empty() {
        None
    } else {
        let ids: HashSet<&str> = pending.iter().map(|p| p.call.id.as_str()).collect();
        let answer_mail: Vec<&Mail> = queued.iter().filter(|m| is_on_time(m, &ids)).collect();
        let answers: HashMap<String, CallResult> =
            answer_mail.iter().flat_map(|m| m.answers.iter().map(|(k, v)| (k.clone(), v.clone()))).collect();
        let ready = queued.iter().any(|m| m.kind == MailKind::Message) || ids.iter().all(|i| answers.contains_key(*i));
        let answer_mail = answer_mail.iter().map(|m| (m.id, m.answers.keys().cloned().collect())).collect();
        Some(Paused { pending, answers, answer_mail, ready, replays: replays.clone() })
    };
    if !interrupted.is_empty() {
        let after = match waiting {
            Some(p) => Position::Paused(p),
            None => Position::Resend { replays: Vec::new() },
        };
        return Ok(Position::Interrupted { calls: interrupted, after: Box::new(after) });
    }
    if let Some(p) = waiting {
        return Ok(Position::Paused(p));
    }
    let resend_request = matches!(last, Some(m @ Message::User(_)) if !is_stopped(m));
    if !replays.is_empty() || resend_request {
        return Ok(Position::Resend { replays });
    }
    Ok(if queued.is_empty() { Position::Idle } else { Position::Fresh })
}

/// 这封回答信件针对的调用都还挂着：作为那些调用的结果交回。否则它是迟到的，作为文字交给模型。
pub fn is_on_time(mail: &Mail, pending: &HashSet<&str>) -> bool {
    mail.kind == MailKind::Answer && mail.answers.keys().all(|k| pending.contains(k.as_str()))
}

/// 交给运行的挂起调用结果。有回答的用回答（replaced 里另有结果的用那个，如批准之前已交过运行、不再执行的「被中断」）；
/// 其余（来了新消息，或这次只回答了一部分）用那一种自己的「跳过」结果。
pub fn deferred_results(
    paused: &Paused,
    replaced: &HashMap<String, Resolution>,
) -> Result<HashMap<String, Resolution>> {
    let given: Vec<&CallResult> = paused.answers.values().collect();
    let mut results = HashMap::new();
    for p in &paused.pending {
        let id = &p.call.id;
        let r = match paused.answers.get(id) {
            None => skipped(p.kind, &given),
            Some(a) if a.kind() != p.kind => {
                return Err(broken(format!("call {id} of {} got an answer for {}", p.call.name, a.kind().as_str())));
            }
            Some(a) => replaced.get(id).cloned().unwrap_or_else(|| a.resolution()),
        };
        results.insert(id.clone(), r);
    }
    if let Some(stray) = replaced.keys().find(|k| !paused.answers.contains_key(*k) || paused.get(k).is_none()) {
        panic!("replaced result for {stray}, which is not an answered pending call");
    }
    Ok(results)
}

/// 用户停止时怎么关闭没出结果的调用：
/// - stopped：补「已停止」——被打断的、能重放的，以及停止后不再挂着的挂起调用；
/// - answered：停止后本该仍挂着、但回答已提交还排着的调用及其结果：回答是停止前用户已经给出的部分，照常写进历史；
/// - used：这些回答所在的信件，随之记为已处理。其余仍挂着、没有回答的调用照旧挂着，用户仍可回答。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct StopClosing {
    pub stopped: Vec<ToolCall>,
    pub answered: Vec<ToolResult>,
    pub used: Vec<i64>,
}

pub fn closing_on_stop(at: &Position) -> StopClosing {
    match at {
        Position::Interrupted { calls, after } => {
            let mut rest = closing_on_stop(after);
            let mut stopped = calls.clone();
            stopped.append(&mut rest.stopped);
            StopClosing { stopped, ..rest }
        }
        Position::Resend { replays } => StopClosing { stopped: replays.clone(), ..Default::default() },
        Position::Paused(p) => {
            let mut closing = StopClosing { stopped: p.replays.clone(), ..Default::default() };
            for pc in &p.pending {
                if !pc.kind.kept_open_on_stop() {
                    closing.stopped.push(pc.call.clone());
                    continue;
                }
                let Some(answer) = p.answers.get(&pc.call.id) else { continue };
                let Resolution::Result { content, is_error } = answer.resolution() else {
                    panic!("{} is kept open on stop but its answer is not a plain result", pc.call.name);
                };
                closing.answered.push(ToolResult {
                    call_id: pc.call.id.clone(),
                    tool_name: pc.call.name.clone(),
                    content,
                    is_error,
                    metadata: None,
                });
            }
            // 整封信记为已处理：同一封里对等批准调用的决定也算处理了（那个调用已补「已停止」）
            let answered: HashSet<&str> = closing.answered.iter().map(|r| r.call_id.as_str()).collect();
            closing.used = p
                .answer_mail
                .iter()
                .filter(|(_, calls)| calls.iter().any(|c| answered.contains(c.as_str())))
                .map(|(id, _)| *id)
                .collect();
            closing
        }
        Position::Idle | Position::Fresh => StopClosing::default(),
    }
}

/// 还没有结果、等用户的挂起调用（提问与等批准的调用）分成现在能回答的，与回答已提交、还在信箱里排着的。界面展示与投信
/// 校验都经过这里。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct OpenAsks {
    /// 现在能回答的提问：调用 id 与问题，按出现顺序
    pub answerable: Vec<(String, Vec<Question>)>,
    /// 现在能批准的调用：调用 id 与工具名
    pub approvable: Vec<(String, String)>,
    pub submitted: HashSet<String>,
    /// 决定已提交、还排着的等批准的调用（submitted 的子集）
    pub approvals_submitted: HashSet<String>,
}

impl OpenAsks {
    pub fn question(&self, call_id: &str) -> Option<&[Question]> {
        self.answerable.iter().find(|(k, _)| k == call_id).map(|(_, q)| q.as_slice())
    }

    pub fn can_approve(&self, call_id: &str) -> bool {
        self.approvable.iter().any(|(k, _)| k == call_id)
    }

    pub fn is_empty(&self) -> bool {
        self.answerable.is_empty() && self.approvable.is_empty()
    }
}

pub fn open_asks_of(at: &Position, queued: &[Mail]) -> OpenAsks {
    let submitted: HashSet<String> =
        queued.iter().filter(|m| m.kind == MailKind::Answer).flat_map(|m| m.answers.keys().cloned()).collect();
    let mut asks = OpenAsks { submitted, ..Default::default() };
    let Some(p) = at.paused() else { return asks };
    for pc in &p.pending {
        let id = &pc.call.id;
        match pc.kind {
            Waiting::AskUser if !asks.submitted.contains(id) => {
                asks.answerable.push((id.clone(), questions_of(&pc.call.args)));
            }
            Waiting::Approval if asks.submitted.contains(id) => {
                asks.approvals_submitted.insert(id.clone());
            }
            Waiting::Approval => asks.approvable.push((id.clone(), pc.call.name.clone())),
            Waiting::AskUser | Waiting::AskAgent => {}
        }
    }
    asks
}
