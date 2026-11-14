//! 挂起调用的三种回答（回答信件 `mail.answers` 的值）：用户对提问（ask_user）的回答、被问的 agent 交回的结局（ask_agent）、
//! 用户对等批准的调用的决定。每种自己给出：交给模型的结果、没等到回答就继续时的结果、调用结束后才到的回答怎么写。
//! 挂起、唤醒与交回的规则在 [`crate::pending`]。存下的形状是 `{"tool": ..., "result": ...}`。

use agenter::{Approval, Content, Resolution};
use muliao_runtime::conventions::SharedFile;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use muliao_platform::errors::{Result, invalid};

// ---- 提问（ask_user）----

// 提问的长度约束：schema 标注（模型看到的）与 [`AskArgs::check`]（模型可能不守）都只引用这里
const LABEL: (usize, usize) = (1, 80);
const DESCRIPTION_MAX: usize = 300;
const QUESTION: (usize, usize) = (1, 500);
const HEADER: (usize, usize) = (1, 20);
const OPTIONS: (usize, usize) = (2, 6);
const QUESTIONS: (usize, usize) = (1, 4);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct QuestionOption {
    /// Short choice text shown on the button.
    #[schemars(length(min = LABEL.0, max = LABEL.1))]
    pub label: String,
    /// What choosing it means, if not obvious.
    #[serde(default)]
    #[schemars(length(max = DESCRIPTION_MAX))]
    pub description: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct Question {
    /// The full question.
    #[schemars(length(min = QUESTION.0, max = QUESTION.1))]
    pub question: String,
    /// Very short label, e.g. 'Timeframe'.
    #[schemars(length(min = HEADER.0, max = HEADER.1))]
    pub header: String,
    #[schemars(length(min = OPTIONS.0, max = OPTIONS.1))]
    pub options: Vec<QuestionOption>,
    /// Allow choosing several options.
    #[serde(default)]
    pub multi_select: bool,
}

/// ask_user 的参数，也是读回调用时的解析类型：约束只在上面的常量里写。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct AskArgs {
    /// The questions; set multi_select when several may apply.
    #[schemars(length(min = QUESTIONS.0, max = QUESTIONS.1))]
    pub questions: Vec<Question>,
}

/// 长度在 (min, max) 里（含两端）。
fn within(n: usize, (min, max): (usize, usize)) -> bool {
    (min..=max).contains(&n)
}

impl AskArgs {
    /// 长度约束（schema 里也标了，模型可能不守）。
    pub(crate) fn check(&self) -> Result<(), String> {
        let n = self.questions.len();
        if !within(n, QUESTIONS) {
            return Err(format!("ask {}-{} questions, got {n}", QUESTIONS.0, QUESTIONS.1));
        }
        for (i, q) in self.questions.iter().enumerate() {
            let i = i + 1;
            let len = |s: &str| s.chars().count();
            if !within(len(&q.question), QUESTION) {
                return Err(format!("question {i}: `question` must be {}-{} characters", QUESTION.0, QUESTION.1));
            }
            if !within(len(&q.header), HEADER) {
                return Err(format!("question {i}: `header` must be {}-{} characters", HEADER.0, HEADER.1));
            }
            if !within(q.options.len(), OPTIONS) {
                return Err(format!("question {i}: give {}-{} options", OPTIONS.0, OPTIONS.1));
            }
            for o in &q.options {
                if !within(len(&o.label), LABEL) || len(&o.description) > DESCRIPTION_MAX {
                    return Err(format!(
                        "question {i}: option labels are {}-{} characters, descriptions up to {DESCRIPTION_MAX}",
                        LABEL.0, LABEL.1
                    ));
                }
            }
        }
        Ok(())
    }
}

/// 一次 ask_user 调用里的问题，按工具参数的同一个类型与约束解析；参数不合法（被工具退回重试的、生成到一半被打断的）时
/// 为空：这次调用不是提问。
pub(crate) fn questions_of(args: &str) -> Vec<Question> {
    match serde_json::from_str::<AskArgs>(args) {
        Ok(a) if a.check().is_ok() => a.questions,
        _ => Vec::new(),
    }
}

/// 用户对一个问题的回答：选中的选项，和/或「其他」里填的文字。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct QuestionAnswer {
    #[serde(default)]
    pub selected: Vec<String>,
    #[serde(default)]
    pub other: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
pub struct AnsweredQuestion {
    pub question: String,
    pub selected: Vec<String>,
    pub other: Option<String>,
}

/// 一次提问交给模型的结果（也是界面展示已回答提问的来源）。存下、交给模型、接口里的形状是扁平的
/// `{answered, answers, note}`（[`AskResultJson`]），读入时解析成这两种之一。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(into = "AskResultJson", try_from = "AskResultJson")]
pub enum AskResult {
    /// 每题的回答
    Answered(Vec<AnsweredQuestion>),
    /// 用户没回答（发了新消息或只回答了别的提问）：交给模型的说明
    NotAnswered(String),
}

/// 一次提问交给模型的结果的存下形状（模型看到的、库里的历史与回答信件、接口里的都是它）：answered 为假时 note 是说明。
#[derive(Serialize, Deserialize, utoipa::ToSchema)]
struct AskResultJson {
    answered: bool,
    answers: Vec<AnsweredQuestion>,
    note: Option<String>,
}

impl From<AskResult> for AskResultJson {
    fn from(r: AskResult) -> Self {
        match r {
            AskResult::Answered(answers) => AskResultJson { answered: true, answers, note: None },
            AskResult::NotAnswered(note) => AskResultJson { answered: false, answers: Vec::new(), note: Some(note) },
        }
    }
}

impl TryFrom<AskResultJson> for AskResult {
    type Error = &'static str;

    fn try_from(j: AskResultJson) -> std::result::Result<Self, Self::Error> {
        match (j.answered, j.note) {
            (true, _) => Ok(AskResult::Answered(j.answers)),
            (false, Some(note)) => Ok(AskResult::NotAnswered(note)),
            (false, None) => Err("an unanswered ask result must carry a note"),
        }
    }
}

// 接口里就是存下的形状
impl utoipa::PartialSchema for AskResult {
    fn schema() -> utoipa::openapi::RefOr<utoipa::openapi::schema::Schema> {
        <AskResultJson as utoipa::PartialSchema>::schema()
    }
}
impl utoipa::ToSchema for AskResult {
    fn schemas(schemas: &mut Vec<(String, utoipa::openapi::RefOr<utoipa::openapi::schema::Schema>)>) {
        <AskResultJson as utoipa::ToSchema>::schemas(schemas)
    }
}

impl AskResult {
    /// 交给模型的结果正文。
    pub(crate) fn text(&self) -> String {
        serde_json::to_string(self).expect("ask results serialize")
    }

    /// 作为提问调用的结果交给模型的内容（按时交回与停止时保留的回答共用）。
    pub(crate) fn content(&self) -> Vec<Content> {
        vec![Content::text(self.text())]
    }
}

const NOT_ANSWERED: &str = "The user did not answer these questions and sent a new message instead.";
const SKIPPED: &str = "The user answered other questions you asked at the same time, but not these.";

/// 校验用户的回答并整理成交给模型的结果。
pub(crate) fn answer_result(questions: &[Question], answers: &[QuestionAnswer]) -> Result<AskResult> {
    if answers.len() != questions.len() {
        return Err(invalid(format!("expected {} answers, got {}", questions.len(), answers.len())));
    }
    let mut out = Vec::new();
    for (i, (q, a)) in questions.iter().zip(answers).enumerate() {
        let i = i + 1;
        let unknown: Vec<&String> = a.selected.iter().filter(|s| !q.options.iter().any(|o| &o.label == *s)).collect();
        if !unknown.is_empty() {
            return Err(invalid(format!("question {i}: {unknown:?} are not options of {:?}", q.question)));
        }
        let mut seen = std::collections::HashSet::new();
        if !a.selected.iter().all(|s| seen.insert(s)) {
            return Err(invalid(format!("question {i}: an option is selected twice")));
        }
        if !q.multi_select && a.selected.len() > 1 {
            return Err(invalid(format!("question {i} allows one choice, got {}", a.selected.len())));
        }
        let other = a.other.as_deref().map(str::trim).unwrap_or("");
        if a.selected.is_empty() && other.is_empty() {
            return Err(invalid(format!("question {i} is not answered")));
        }
        out.push(AnsweredQuestion {
            question: q.question.clone(),
            selected: a.selected.clone(),
            other: (!other.is_empty()).then(|| other.to_owned()),
        });
    }
    Ok(AskResult::Answered(out))
}

/// 运行停在提问上时给用户的通知正文。
pub(crate) fn questions_waiting_text(questions: &[Question]) -> String {
    let asked: Vec<&str> = questions.iter().map(|q| q.question.as_str()).collect();
    format!("等你回答：{}", asked.join("；"))
}

// ---- 被问的 agent（ask_agent）----

/// ask_agent 答复之前来了新消息：这次调用不再等，模型知道回答之后会作为事件送达。
pub const STILL_WORKING: &str = "A new message arrived before the agent you asked answered. Its answer will reach you as \
     an event in this conversation when it is ready.";

/// 被问的 agent 交回的结局：给发问方模型的文字，它挂给用户的文件（交回时挂到发问方这次调用的结果上，文件就出现在用户
/// 正在看的会话里）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
pub struct AgentAnswer {
    pub text: String,
    #[serde(default)]
    pub attachments: Vec<SharedFile>,
}

// ---- 等批准的调用 ----

pub const DENIED: &str = "The user denied this call; it was not run.";
const NOT_DECIDED: &str = "The user did not approve this call and responded with something else instead; it was not \
     run. Call it again if it is still needed.";
/// 批准了，但这份批准之前已经交给过运行去执行：可能已在执行中被打断，不再执行。
pub const APPROVAL_INTERRUPTED: &str = "The user approved this call, but the run was interrupted (e.g. the service \
     restarted) after it may have started executing, so it was not run again. It may have taken effect: check before \
     calling it again (that asks the user to approve again).";

/// 用户在界面上对等批准的调用的选择。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Choice {
    Approve,
    Deny,
    /// 批准，并且以后同样的调用不再问（记下什么算「同样」由工具决定）
    Always,
}

/// 用户对一次等批准的调用的决定。pin：用户批准的是哪一份（请求批准时工具定下、记在挂起记录里的指纹），批准时
/// 交回工具比对。迁移来的旧信件没有它或为 null（都已处理，不会再交给运行），读入时当作空指纹：工具给的指纹不会是空的，
/// 比对对不上。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
pub struct Decision {
    pub choice: Choice,
    #[serde(default, deserialize_with = "old_pin")]
    pub pin: String,
}

/// 旧信件的 pin（缺省或 null）读成空指纹，见 [`Decision`]。
fn old_pin<'de, D: serde::Deserializer<'de>>(d: D) -> std::result::Result<String, D::Error> {
    Ok(Option::<String>::deserialize(d)?.unwrap_or_default())
}

impl Decision {
    pub(crate) fn approved(&self) -> bool {
        self.choice != Choice::Deny
    }
}

/// 运行停在等批准上时给用户的通知正文。
pub(crate) fn approvals_waiting_text(calls: &[String]) -> String {
    format!("等你批准：{}", calls.join("、"))
}

// ---- 三种合在一起 ----

/// 运行停在哪类挂起的调用上（与回答的 `tool` 同名）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Waiting {
    AskUser,
    AskAgent,
    Approval,
}

impl Waiting {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Waiting::AskUser => "ask_user",
            Waiting::AskAgent => "ask_agent",
            Waiting::Approval => "approval",
        }
    }

    pub(crate) fn parse(s: &str) -> Option<Waiting> {
        [Waiting::AskUser, Waiting::AskAgent, Waiting::Approval].into_iter().find(|w| w.as_str() == s)
    }
}

/// 用户对提问的回答，每题一行（`- 问题: 选项; 其他`）：调用结束后才到的回答交给模型、热历史列出用户的回答，都用这一份。
pub(crate) fn answer_lines(asked: &[&AskResult]) -> Vec<String> {
    asked
        .iter()
        .flat_map(|r| match r {
            AskResult::Answered(answers) => answers.as_slice(),
            AskResult::NotAnswered(_) => &[],
        })
        .map(|a| {
            let given: Vec<&str> = a.selected.iter().map(String::as_str).chain(a.other.as_deref()).collect();
            format!("- {}: {}", a.question, given.join("; "))
        })
        .collect()
}

/// 回答信件里一次调用的结果。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(tag = "tool", content = "result", rename_all = "snake_case")]
pub enum CallResult {
    AskUser(AskResult),
    AskAgent(AgentAnswer),
    Approval(Decision),
}

impl CallResult {
    pub(crate) fn kind(&self) -> Waiting {
        match self {
            CallResult::AskUser(_) => Waiting::AskUser,
            CallResult::AskAgent(_) => Waiting::AskAgent,
            CallResult::Approval(_) => Waiting::Approval,
        }
    }

    /// 交给模型的结果。
    pub(crate) fn resolution(&self) -> Resolution {
        match self {
            CallResult::AskUser(r) => Resolution::Result { content: r.content(), is_error: false },
            CallResult::AskAgent(a) => {
                let mut t = a.text.clone();
                if !a.attachments.is_empty() {
                    let names: Vec<String> = a.attachments.iter().map(|x| format!("{:?}", x.name)).collect();
                    t.push_str(&format!(
                        "\n\n[Shared {} with the user; they are shown with your reply automatically.]",
                        names.join(", ")
                    ));
                }
                text(t)
            }
            CallResult::Approval(d) if d.approved() => {
                Resolution::Approved(Approval { always: d.choice == Choice::Always, pin: d.pin.clone() })
            }
            CallResult::Approval(_) => Resolution::Denied(DENIED.to_owned()),
        }
    }

    /// 调用结束后才到的回答（那次调用已经带着「没回答」或「之后送达」继续了），作为一段文字交给模型，回答不丢。
    pub(crate) fn late_text(results: &[CallResult]) -> String {
        let mut lines = Vec::new();
        let asked: Vec<&AskResult> = results
            .iter()
            .filter_map(|r| match r {
                CallResult::AskUser(a) => Some(a),
                _ => None,
            })
            .collect();
        if !asked.is_empty() {
            lines.push("The user answered questions that had already been closed:".to_owned());
            lines.extend(answer_lines(&asked));
        }
        for r in results {
            match r {
                CallResult::AskAgent(a) => lines.push(format!("An agent you asked earlier answered:\n{}", a.text)),
                CallResult::Approval(d) => lines.push(format!(
                    "The user {} a call that had already been closed without running; it was not run.",
                    if d.approved() { "approved" } else { "denied" }
                )),
                CallResult::AskUser(_) => {}
            }
        }
        lines.join("\n")
    }
}

/// 没等到回答就继续时这一种调用的结果；given 是这次交回的别的回答。
pub(crate) fn skipped(kind: Waiting, given: &[&CallResult]) -> Resolution {
    match kind {
        Waiting::AskUser => {
            let answered_other = given.iter().any(|r| matches!(r, CallResult::AskUser(_)));
            let r = AskResult::NotAnswered(if answered_other { SKIPPED } else { NOT_ANSWERED }.to_owned());
            text(r.text())
        }
        Waiting::AskAgent => text(STILL_WORKING.to_owned()),
        Waiting::Approval => Resolution::Denied(NOT_DECIDED.to_owned()),
    }
}

fn text(t: String) -> Resolution {
    Resolution::Result { content: vec![Content::text(t)], is_error: false }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(multi: bool) -> Question {
        Question {
            question: "Which?".into(),
            header: "Pick".into(),
            options: vec![
                QuestionOption { label: "a".into(), description: String::new() },
                QuestionOption { label: "b".into(), description: String::new() },
            ],
            multi_select: multi,
        }
    }

    #[test]
    fn answers_are_checked_against_the_questions() {
        let ok = answer_result(&[q(false)], &[QuestionAnswer { selected: vec!["a".into()], other: None }]).unwrap();
        assert!(matches!(ok, AskResult::Answered(_)));
        for bad in [
            QuestionAnswer { selected: vec!["c".into()], other: None },
            QuestionAnswer { selected: vec!["a".into(), "b".into()], other: None },
            QuestionAnswer { selected: vec![], other: Some("  ".into()) },
        ] {
            assert!(answer_result(&[q(false)], &[bad]).is_err());
        }
        assert!(
            answer_result(&[q(true)], &[QuestionAnswer { selected: vec!["a".into(), "a".into()], other: None }])
                .is_err()
        );
    }

    #[test]
    fn stored_shape() {
        let r = CallResult::Approval(Decision { choice: Choice::Always, pin: "p".into() });
        let v = serde_json::to_value(&r).unwrap();
        assert_eq!(v, serde_json::json!({"tool": "approval", "result": {"choice": "always", "pin": "p"}}));
        let back: CallResult =
            serde_json::from_value(serde_json::json!({"tool": "ask_agent", "result": {"text": "x"}})).unwrap();
        assert_eq!(back, CallResult::AskAgent(AgentAnswer { text: "x".into(), attachments: vec![] }));
        // 提问的结果存成扁平的 {answered, answers, note}，模型看到的也是它
        let skipped = AskResult::NotAnswered("n".into());
        let flat = serde_json::json!({"answered": false, "answers": [], "note": "n"});
        assert_eq!(serde_json::to_value(&skipped).unwrap(), flat);
        assert_eq!(serde_json::from_value::<AskResult>(flat).unwrap(), skipped);
        let answered = serde_json::json!({"answered": true, "answers": [], "note": null});
        assert_eq!(serde_json::from_value::<AskResult>(answered).unwrap(), AskResult::Answered(vec![]));
        // 旧信件里的决定没有 pin
        let old: Decision = serde_json::from_value(serde_json::json!({"choice": "approve"})).unwrap();
        assert_eq!(old.pin, "");
        let old: Decision = serde_json::from_value(serde_json::json!({"choice": "approve", "pin": null})).unwrap();
        assert_eq!(old.pin, "");
    }

    #[test]
    fn invalid_ask_args_are_not_questions() {
        assert!(questions_of("{\"questions\": []}").is_empty());
        assert!(questions_of("not json").is_empty());
        let one = serde_json::to_string(&AskArgs { questions: vec![q(false)] }).unwrap();
        assert_eq!(questions_of(&one).len(), 1);
    }
}
