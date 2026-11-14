//! 挂起的工具调用：模型调用了一个要等别人回信的工具（ask_user 等用户、ask_agent 等另一个 agent），或工具执行前要用户
//! 批准。运行停在这里正常结束；回信是一封回答信件，作为那次调用的结果交回，模型看到的就是「我问了，对方这样答」。
//! 设计见 docs/architecture.md「挂起的调用」。
//!
//! - 一次调用在不在等、等的是哪一种，只看挂起时记下的那一行（[`crate::paused_calls`]，#823），不按工具名认；三种的差别
//!   只写在 [`Waiting`] 的方法里，各处查它。
//! - 会话停在哪（[`position`]，纯函数，唯一的判断）：空闲、新开、从历史续跑、被打断、等回答。由历史、挂起记录与信箱
//!   推导；actor、唤醒、停止、等用户的调用、交回结局都 match 它。
//! - 什么时候继续（[`Paused::ready`]）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件留在信箱里等它们
//!   一起交给模型。

use muliao_platform::ids::MailId;
use std::collections::{HashMap, HashSet};

use agenter::{Message, Resolution, ToolCall, ToolResult};

use crate::answers::{CallResult, Question, Waiting, questions_of, skipped};
use crate::conventions::{is_stopped, unanswered_calls};
use crate::mailbox::{Mail, MailBody};
use crate::paused_calls::PausedKinds;
use muliao_platform::errors::{Result, broken};

impl Waiting {
    /// 用户停止时不补「已停止」，仍挂着等回答（只有提问，它的回答是 [`CallResult::AskUser`]）。
    fn kept_open_on_stop(self) -> bool {
        self == Waiting::AskUser
    }
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
    pub answer_mail: Vec<(MailId, String)>,
    /// 现在能不能继续
    pub ready: bool,
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
    /// 从历史续跑：历史停在一条没得到回复、没标停止的请求上，重发它
    Resend,
    Paused(Paused),
    /// 被打断：最后的回复里有没出结果、也没记为挂起的调用（执行工具时被打断）。它们不重新执行，先补上「被中断」的结果
    /// 再看 after
    Interrupted {
        calls: Vec<ToolCall>,
        after: AfterInterrupted,
    },
}

/// 被打断的那一步补完「被中断」的结果之后停在哪。
#[derive(Debug, Clone, PartialEq)]
pub enum AfterInterrupted {
    /// 没有挂起的调用：把补上的结果发给模型
    Resend,
    /// 同一条回复里还有挂起的调用
    Paused(Paused),
}

impl From<AfterInterrupted> for Position {
    fn from(after: AfterInterrupted) -> Self {
        match after {
            AfterInterrupted::Resend => Position::Resend,
            AfterInterrupted::Paused(p) => Position::Paused(p),
        }
    }
}

impl Position {
    /// 挂着的调用（被打断时是补完结果之后仍挂着的）。
    pub(crate) fn paused(&self) -> Option<&Paused> {
        match self {
            Position::Paused(p) | Position::Interrupted { after: AfterInterrupted::Paused(p), .. } => Some(p),
            Position::Interrupted { after: AfterInterrupted::Resend, .. }
            | Position::Idle
            | Position::Fresh
            | Position::Resend => None,
        }
    }

    /// 停在挂起的调用上、还不能继续：等回答或新消息，事件等它们一起交给模型（唤醒据此不跑）。
    pub(crate) fn waiting_for_answers(&self) -> bool {
        self.paused().is_some_and(|p| !p.ready)
    }
}

/// 会话现在停在哪（纯函数）。history 至少要含最后一条回复起的那一段；paused 是这个会话挂起过的调用；queued 是信箱里
/// 排着的信件。
pub(crate) fn position(history: &[Message], paused: &PausedKinds, queued: &[Mail]) -> Result<Position> {
    let mut pending: Vec<PendingCall> = Vec::new();
    let mut interrupted: Vec<ToolCall> = Vec::new();
    for c in unanswered_calls(history)? {
        match paused.get(&c.id) {
            Some(&kind) => pending.push(PendingCall { call: c, kind }),
            None => interrupted.push(c),
        }
    }
    let waiting = if pending.is_empty() {
        None
    } else {
        let ids: HashSet<&str> = pending.iter().map(|p| p.call.id.as_str()).collect();
        let on_time: Vec<(MailId, &str, &CallResult)> =
            queued.iter().filter_map(|m| on_time_answer(m, &ids).map(|(c, r)| (m.id, c, r))).collect();
        let answers: HashMap<String, CallResult> =
            on_time.iter().map(|(_, c, r)| ((*c).to_owned(), (*r).clone())).collect();
        let ready = queued.iter().any(|m| matches!(m.body, MailBody::Message { .. }))
            || ids.iter().all(|i| answers.contains_key(*i));
        let answer_mail = on_time.into_iter().map(|(id, c, _)| (id, c.to_owned())).collect();
        Some(Paused { pending, answers, answer_mail, ready })
    };
    if !interrupted.is_empty() {
        let after = match waiting {
            Some(p) => AfterInterrupted::Paused(p),
            None => AfterInterrupted::Resend,
        };
        return Ok(Position::Interrupted { calls: interrupted, after });
    }
    if let Some(p) = waiting {
        return Ok(Position::Paused(p));
    }
    if matches!(history.last(), Some(m @ Message::User(_)) if !is_stopped(m)) {
        return Ok(Position::Resend);
    }
    Ok(if queued.is_empty() { Position::Idle } else { Position::Fresh })
}

/// 这封回答信件针对的调用还挂着：它的回答作为那次调用的结果交回（返回调用 id 与回答）。否则它是迟到的，作为文字交给模型。
pub(crate) fn on_time_answer<'a>(mail: &'a Mail, pending: &HashSet<&str>) -> Option<(&'a str, &'a CallResult)> {
    mail.body.answer().filter(|(c, _)| pending.contains(c))
}

/// 交给运行的挂起调用结果。有回答的用回答，等批准的调用的结果由 [`crate::paused_calls::take_in`] 对着挂起记录给出
/// （decided，每个批准决定都在里面）；其余（来了新消息，或这次只回答了一部分）用那一种自己的「跳过」结果。
pub(crate) fn deferred_results(
    paused: &Paused,
    mut decided: HashMap<String, Resolution>,
) -> Result<HashMap<String, Resolution>> {
    let given: Vec<&CallResult> = paused.answers.values().collect();
    let mut results = HashMap::new();
    for p in &paused.pending {
        let id = &p.call.id;
        let r = match (p.kind, paused.answers.get(id)) {
            (_, None) => skipped(p.kind, &given),
            (Waiting::AskUser, Some(CallResult::AskUser(r))) => r.resolution(),
            (Waiting::AskAgent, Some(CallResult::AskAgent(a))) => a.resolution(),
            (Waiting::Approval, Some(CallResult::Approval(_))) => {
                decided.remove(id).expect("take_in resolves every decision among the answers")
            }
            (_, Some(a)) => {
                return Err(broken(format!("call {id} of {} got an answer for {}", p.call.name, a.kind())));
            }
        };
        results.insert(id.clone(), r);
    }
    Ok(results)
}

/// 用户停止时怎么关闭没出结果的调用：
/// - stopped：补「已停止」——被打断的，以及停止后不再挂着的挂起调用；
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
            let mut rest = match after {
                AfterInterrupted::Resend => StopClosing::default(),
                AfterInterrupted::Paused(p) => closing_paused(p),
            };
            let mut stopped = calls.clone();
            stopped.append(&mut rest.stopped);
            StopClosing { stopped, ..rest }
        }
        Position::Paused(p) => closing_paused(p),
        Position::Resend | Position::Idle | Position::Fresh => StopClosing::default(),
    }
}

fn closing_paused(p: &Paused) -> StopClosing {
    let mut closing = StopClosing::default();
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
    // 写进结果的回答所在的信记为已处理
    let answered: HashSet<&str> = closing.answered.iter().map(|r| r.call_id.as_str()).collect();
    closing.used =
        p.answer_mail.iter().filter(|(_, call)| answered.contains(call.as_str())).map(|(id, _)| *id).collect();
    closing
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
        .filter_map(|m| m.body.answer())
        .filter(|(id, r)| r.kind() != Waiting::Approval || pending(id))
        .map(|(id, r)| (id.to_owned(), OpenAsk::Submitted(r.kind())))
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
    use muliao_platform::ids::JobId;

    use agenter::{Request, RequestPart, Response, ResponsePart};
    use chrono::Utc;

    use super::*;
    use crate::answers::{AgentAnswer, AskResult};
    use crate::mailbox::MailState;
    use crate::origins::Origin;
    use crate::tools::ASK_USER;

    const ASK_AGENT: &str = "ask_agent";

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

    fn answered(id: &str) -> MailBody {
        MailBody::Answer { call_id: id.to_owned(), result: CallResult::AskUser(AskResult::Answered(vec![])) }
    }

    fn kinds(of: &[(&str, Waiting)]) -> PausedKinds {
        of.iter().map(|(id, k)| ((*id).to_owned(), *k)).collect()
    }

    #[test]
    fn idle_fresh_and_resend() {
        let none = PausedKinds::new();
        assert_eq!(position(&[response(vec![])], &none, &[]).unwrap(), Position::Idle);
        let q = [mail(1, message())];
        assert_eq!(position(&[response(vec![])], &none, &q).unwrap(), Position::Fresh);
        assert_eq!(position(&[user()], &none, &[]).unwrap(), Position::Resend);
        let mut stopped = user();
        crate::conventions::mark_stopped(&mut stopped);
        assert_eq!(position(&[stopped], &none, &[]).unwrap(), Position::Idle);
    }

    #[test]
    fn asks_wait_until_answered_or_a_new_message() {
        let asked = kinds(&[("q1", Waiting::AskUser)]);
        let h = [response(vec![call("q1", ASK_USER, ASK)])];
        let at = position(
            &h,
            &asked,
            &[mail(
                1,
                MailBody::Event {
                    source: crate::work::EventSource::Job { job_id: JobId::new(1) },
                    content: String::new(),
                    check_in: false,
                },
            )],
        )
        .unwrap();
        assert!(at.waiting_for_answers(), "events wait with the question");
        let at = position(&h, &asked, &[mail(1, answered("q1"))]).unwrap();
        assert!(at.paused().unwrap().ready);
        let at = position(&h, &asked, &[mail(1, message())]).unwrap();
        assert!(at.paused().unwrap().ready, "a new message skips the question");
    }

    /// 在不在等只看挂起记录，不看工具名（#823）：没记下的提问、问 agent 都是执行到一半被打断的调用，补「被中断」；
    /// 以前没交出去的 ask_agent 会原样重放、没记下的提问按名字一直挂着。
    #[test]
    fn only_recorded_calls_wait_and_the_rest_are_interrupted() {
        let none = PausedKinds::new();
        for name in [ASK_USER, ASK_AGENT, "run_shell"] {
            let h = [response(vec![call("c1", name, ASK)])];
            let Position::Interrupted { calls, after } = position(&h, &none, &[]).unwrap() else { panic!("{name}") };
            assert_eq!((calls.len(), after), (1, AfterInterrupted::Resend), "{name}");
        }
        let h = [response(vec![call("a1", ASK_AGENT, "{}")])];
        let asked = kinds(&[("a1", Waiting::AskAgent)]);
        assert!(position(&h, &asked, &[]).unwrap().waiting_for_answers());
        let answer = MailBody::Answer {
            call_id: "a1".to_owned(),
            result: CallResult::AskAgent(AgentAnswer { text: "x".into(), attachments: vec![] }),
        };
        assert!(position(&h, &asked, &[mail(1, answer)]).unwrap().paused().unwrap().ready);
        // 同一条回复里记下的仍挂着，没记下的补「被中断」
        let mixed = [response(vec![call("a1", ASK_AGENT, "{}"), call("c1", "run_shell", "{}")])];
        let Position::Interrupted { calls, after } = position(&mixed, &asked, &[]).unwrap() else { panic!() };
        assert_eq!(calls, vec![call("c1", "run_shell", "{}")]);
        assert!(matches!(after, AfterInterrupted::Paused(p) if p.pending.len() == 1));
        // 等批准
        let gated = [response(vec![call("c1", "skill_run", "{}")])];
        let at = position(&gated, &kinds(&[("c1", Waiting::Approval)]), &[]).unwrap();
        assert_eq!(open_asks_of(&at, &[]).0, vec![("c1".to_owned(), OpenAsk::Approval("skill_run".to_owned()))]);
        let closing = closing_on_stop(&at);
        assert_eq!(closing.stopped.len(), 1, "stopping does not keep calls waiting for approval");
    }

    #[test]
    fn stop_keeps_unanswered_questions_and_writes_submitted_answers() {
        let asked = kinds(&[("q1", Waiting::AskUser), ("q2", Waiting::AskUser)]);
        let h = [response(vec![call("q1", ASK_USER, ASK), call("q2", ASK_USER, ASK)])];
        let at = position(&h, &asked, &[mail(7, answered("q1"))]).unwrap();
        let closing = closing_on_stop(&at);
        assert!(closing.stopped.is_empty());
        assert_eq!(closing.answered.iter().map(|r| r.call_id.as_str()).collect::<Vec<_>>(), ["q1"]);
        assert_eq!(closing.used, [MailId::new(7)]);
    }
}
