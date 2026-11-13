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

use muliao_platform::ids::MailId;
use std::collections::{BTreeMap, HashMap, HashSet};

use agenter::{Message, Resolution, ToolCall, ToolResult};

use crate::answers::{CallResult, Question, Waiting, questions_of, skipped};
use crate::conventions::{is_stopped, unanswered_calls};
use crate::mailbox::{Mail, MailBody};
use muliao_platform::errors::{Result, broken};

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

    /// 用户停止时不补「已停止」，仍挂着等回答（只有提问，它的回答是 [`CallResult::AskUser`]）。
    fn kept_open_on_stop(self) -> bool {
        self == Waiting::AskUser
    }
}

/// 这次调用若挂起，是哪一种：记过等批准的是等批准，否则按工具名认。提问的参数不合法（没有问题）时为 None：这样的
/// 调用没执行过，不是在等用户回答，停止与续跑都按普通调用补结果。
pub(crate) fn kind_of(call: &ToolCall, approvals: &HashSet<String>) -> Option<Waiting> {
    if approvals.contains(&call.id) {
        return Some(Waiting::Approval);
    }
    match call.name.as_str() {
        ASK_USER if !questions_of(&call.args).is_empty() => Some(Waiting::AskUser),
        ASK_AGENT => Some(Waiting::AskAgent),
        _ => None,
    }
}

/// 一步停在挂起的调用上时（agenter 的 `Step::Paused`）各调用是哪一种，同 [`kind_of`]：挂起的只能是 ask_user、ask_agent，
/// 请求批准的不能是它们（按工具名认的与按记录认的会认不清）。不符合是工具与这里的约定不一致。
pub(crate) fn paused_calls(deferred: Vec<ToolCall>, approvals: Vec<ToolCall>) -> Result<Vec<PendingCall>> {
    let none = HashSet::new();
    let mut out = Vec::new();
    for call in deferred {
        match kind_of(&call, &none) {
            Some(kind @ (Waiting::AskUser | Waiting::AskAgent)) => out.push(PendingCall { call, kind }),
            _ => return Err(broken(format!("run deferred an unexpected tool call: {}", call.name))),
        }
    }
    for call in approvals {
        if kind_of(&call, &none).is_some() {
            return Err(broken(format!("a deferred tool asked for approval: {}", call.name)));
        }
        out.push(PendingCall { call, kind: Waiting::Approval });
    }
    Ok(out)
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
    pub answer_mail: Vec<(MailId, Vec<String>)>,
    /// 现在能不能继续
    pub ready: bool,
    /// 同一条回复里没出结果、不挂起、能原样重新执行的调用（还没交出去的 ask_agent）：继续时执行，停止时补「已停止」
    pub replays: Vec<ToolCall>,
}

impl Paused {
    pub(crate) fn get(&self, call_id: &str) -> Option<&PendingCall> {
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
    pub(crate) fn paused(&self) -> Option<&Paused> {
        match self {
            Position::Paused(p) => Some(p),
            Position::Interrupted { after, .. } => after.paused(),
            _ => None,
        }
    }

    /// 停在挂起的调用上、还不能继续：等回答或新消息，事件等它们一起交给模型（唤醒据此不跑）。
    pub(crate) fn waiting_for_answers(&self) -> bool {
        self.paused().is_some_and(|p| !p.ready)
    }
}

/// 会话现在停在哪（纯函数）。history 至少要含最后一条回复起的那一段；handed 是交出去的永久事实；queued 是信箱里排着的
/// 信件。
pub(crate) fn position(history: &[Message], handed: &HandedOut, queued: &[Mail]) -> Result<Position> {
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
        let answer_mail: Vec<(MailId, &BTreeMap<String, CallResult>)> =
            queued.iter().filter_map(|m| on_time_answers(m, &ids).map(|a| (m.id, a))).collect();
        let answers: HashMap<String, CallResult> =
            answer_mail.iter().flat_map(|(_, a)| a.iter().map(|(k, v)| (k.clone(), v.clone()))).collect();
        let ready = queued.iter().any(|m| matches!(m.body, MailBody::Message { .. }))
            || ids.iter().all(|i| answers.contains_key(*i));
        let answer_mail = answer_mail.into_iter().map(|(id, a)| (id, a.keys().cloned().collect())).collect();
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

/// 这封回答信件针对的调用都还挂着：它的回答作为那些调用的结果交回（返回这些回答）。否则它是迟到的，作为文字交给模型。
pub(crate) fn on_time_answers<'a>(mail: &'a Mail, pending: &HashSet<&str>) -> Option<&'a BTreeMap<String, CallResult>> {
    mail.body.answers().filter(|a| a.keys().all(|k| pending.contains(k.as_str())))
}

/// 交给运行的挂起调用结果。有回答的用回答（replaced 里另有结果的用那个，如批准之前已交过运行、不再执行的「被中断」）；
/// 其余（来了新消息，或这次只回答了一部分）用那一种自己的「跳过」结果。
pub(crate) fn deferred_results(
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
    pub used: Vec<MailId>,
}

pub(crate) fn closing_on_stop(at: &Position) -> StopClosing {
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
                let Some(CallResult::AskUser(answer)) = p.answers.get(&pc.call.id) else { continue };
                closing.answered.push(ToolResult {
                    call_id: pc.call.id.clone(),
                    tool_name: pc.call.name.clone(),
                    content: answer.content(),
                    is_error: false,
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

/// 一次还没有结果、等用户的挂起调用现在怎样。
#[derive(Debug, Clone, PartialEq)]
pub enum OpenAsk {
    /// 现在能回答的提问
    Question(Vec<Question>),
    /// 现在能批准的调用（工具名）
    Approval(String),
    /// 回答或决定已提交、还在信箱里排着（是哪一种调用的）
    Submitted(Waiting),
}

/// 还没有结果、等用户的挂起调用（提问与等批准的调用），按出现顺序：现在能回答、能批准的，与已提交、还排着的。界面展示与
/// 投信校验都经过这里。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct OpenAsks(pub Vec<(String, OpenAsk)>);

impl OpenAsks {
    pub(crate) fn get(&self, call_id: &str) -> Option<&OpenAsk> {
        self.0.iter().find(|(k, _)| k == call_id).map(|(_, a)| a)
    }

    pub(crate) fn question(&self, call_id: &str) -> Option<&[Question]> {
        match self.get(call_id)? {
            OpenAsk::Question(q) => Some(q),
            _ => None,
        }
    }

    pub(crate) fn can_approve(&self, call_id: &str) -> bool {
        matches!(self.get(call_id), Some(OpenAsk::Approval(_)))
    }

    /// 有等用户现在操作的（能回答或能批准的）。
    pub(crate) fn any_open(&self) -> bool {
        self.0.iter().any(|(_, a)| !matches!(a, OpenAsk::Submitted(_)))
    }
}

pub(crate) fn open_asks_of(at: &Position, queued: &[Mail]) -> OpenAsks {
    // 已提交的批准只算还挂着的调用（迟到的不算）；提问的「已提交」不看挂没挂着：运行还没停下时也显示已提交
    let pending = |id: &str| at.paused().is_some_and(|p| p.pending.iter().any(|pc| pc.call.id == id));
    let submitted: Vec<(String, OpenAsk)> = queued
        .iter()
        .filter_map(|m| m.body.answers())
        .flatten()
        .filter(|(id, r)| r.kind() != Waiting::Approval || pending(id))
        .map(|(id, r)| (id.clone(), OpenAsk::Submitted(r.kind())))
        .collect();
    let is_submitted = |id: &str| submitted.iter().any(|(k, _)| k == id);
    let mut open = Vec::new();
    if let Some(p) = at.paused() {
        for pc in &p.pending {
            let id = &pc.call.id;
            match pc.kind {
                Waiting::AskUser if !is_submitted(id) => {
                    open.push((id.clone(), OpenAsk::Question(questions_of(&pc.call.args))));
                }
                Waiting::Approval if !is_submitted(id) => {
                    open.push((id.clone(), OpenAsk::Approval(pc.call.name.clone())));
                }
                Waiting::AskUser | Waiting::Approval | Waiting::AskAgent => {}
            }
        }
    }
    open.extend(submitted);
    OpenAsks(open)
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use agenter::{Request, RequestPart, Response, ResponsePart};
    use chrono::Utc;

    use super::*;
    use crate::answers::{AgentAnswer, AskResult};
    use crate::mailbox::MailState;
    use crate::origins::Origin;

    fn call(id: &str, name: &str, args: &str) -> ToolCall {
        ToolCall { id: id.into(), name: name.into(), args: args.into(), meta: None }
    }

    const ASK: &str = r#"{"questions":[{"question":"q","header":"h","options":[{"label":"a"},{"label":"b"}]}]}"#;

    fn response(calls: Vec<ToolCall>) -> Message {
        Message::Assistant(Response {
            parts: calls.into_iter().map(ResponsePart::ToolCall).collect(),
            protocol: "anthropic".into(),
            model: "m".into(),
            response_id: None,
            finish_reason: None,
            usage: Default::default(),
            timestamp: Utc::now(),
            metadata: None,
        })
    }

    fn user() -> Message {
        Message::User(Request {
            parts: vec![RequestPart::Text { text: "hi".into() }],
            timestamp: Utc::now(),
            metadata: None,
        })
    }

    fn mail(id: i64, body: MailBody) -> Mail {
        Mail {
            id: MailId::new(id),
            session_id: muliao_platform::ids::SessionId::new("s"),
            body,
            awaited: false,
            state: MailState::Queued,
            taken_by: None,
            created_at: Utc::now(),
        }
    }

    fn message() -> MailBody {
        MailBody::Message { origin: Origin::User, content: String::new(), attachments: vec![], delegation_id: None }
    }

    fn answer_body(answers: BTreeMap<String, CallResult>) -> MailBody {
        MailBody::Answer { answers, from_user: true }
    }

    fn answered(id: &str) -> BTreeMap<String, CallResult> {
        BTreeMap::from([(
            id.to_owned(),
            CallResult::AskUser(AskResult { answered: true, answers: vec![], note: None }),
        )])
    }

    #[test]
    fn idle_fresh_and_resend() {
        let none = HandedOut::default();
        assert_eq!(position(&[response(vec![])], &none, &[]).unwrap(), Position::Idle);
        let q = [mail(1, message())];
        assert_eq!(position(&[response(vec![])], &none, &q).unwrap(), Position::Fresh);
        assert_eq!(position(&[user()], &none, &[]).unwrap(), Position::Resend { replays: vec![] });
        let mut stopped = user();
        crate::conventions::mark_stopped(&mut stopped);
        assert_eq!(position(&[stopped], &none, &[]).unwrap(), Position::Idle);
    }

    #[test]
    fn asks_wait_until_answered_or_a_new_message() {
        let none = HandedOut::default();
        let h = [response(vec![call("q1", ASK_USER, ASK)])];
        let at = position(
            &h,
            &none,
            &[mail(
                1,
                MailBody::Event {
                    source: crate::work::EventSource::Job { job_id: 1 },
                    content: String::new(),
                    check_in: false,
                },
            )],
        )
        .unwrap();
        assert!(at.waiting_for_answers(), "events wait with the question");
        let at = position(&h, &none, &[mail(1, answer_body(answered("q1")))]).unwrap();
        assert!(at.paused().unwrap().ready);
        let at = position(&h, &none, &[mail(1, message())]).unwrap();
        assert!(at.paused().unwrap().ready, "a new message skips the question");
        // 参数不合法的提问不是提问：被打断的普通调用
        let bad = [response(vec![call("q1", ASK_USER, "{}")])];
        assert!(matches!(position(&bad, &none, &[]).unwrap(), Position::Interrupted { .. }));
    }

    #[test]
    fn ask_agent_replays_until_handed_out_and_other_calls_are_interrupted() {
        let h = [response(vec![call("a1", ASK_AGENT, "{}")])];
        let none = HandedOut::default();
        assert_eq!(position(&h, &none, &[]).unwrap(), Position::Resend { replays: vec![call("a1", ASK_AGENT, "{}")] });
        let handed = HandedOut { delegated: ["a1".to_owned()].into(), approvals: Default::default() };
        let at = position(&h, &handed, &[]).unwrap();
        assert!(at.waiting_for_answers());
        let answer = BTreeMap::from([(
            "a1".to_owned(),
            CallResult::AskAgent(AgentAnswer { text: "x".into(), attachments: vec![] }),
        )]);
        assert!(position(&h, &handed, &[mail(1, answer_body(answer))]).unwrap().paused().unwrap().ready);
        // 一个不能重放就整步补「被中断」
        let mixed = [response(vec![call("a1", ASK_AGENT, "{}"), call("c1", "run_shell", "{}")])];
        let Position::Interrupted { calls, after } = position(&mixed, &none, &[]).unwrap() else { panic!() };
        assert_eq!(calls.len(), 2);
        assert_eq!(*after, Position::Resend { replays: vec![] });
        // 等批准的按记录认
        let gated = [response(vec![call("c1", "skill_run", "{}")])];
        let approvals = HandedOut { delegated: Default::default(), approvals: ["c1".to_owned()].into() };
        let at = position(&gated, &approvals, &[]).unwrap();
        assert_eq!(open_asks_of(&at, &[]).0, vec![("c1".to_owned(), OpenAsk::Approval("skill_run".to_owned()))]);
        let closing = closing_on_stop(&at);
        assert_eq!(closing.stopped.len(), 1, "stopping does not keep calls waiting for approval");
    }

    #[test]
    fn stop_keeps_unanswered_questions_and_writes_submitted_answers() {
        let none = HandedOut::default();
        let h = [response(vec![call("q1", ASK_USER, ASK), call("q2", ASK_USER, ASK)])];
        let at = position(&h, &none, &[mail(7, answer_body(answered("q1")))]).unwrap();
        let closing = closing_on_stop(&at);
        assert!(closing.stopped.is_empty());
        assert_eq!(closing.answered.iter().map(|r| r.call_id.as_str()).collect::<Vec<_>>(), ["q1"]);
        assert_eq!(closing.used, [MailId::new(7)]);
    }
}
