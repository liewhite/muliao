//! 把 agenter 消息投影成展示条目（[`Item`]）：会话页的历史、运行中的实时条目、压缩的文字记录共用这一份投影，前端只渲染
//! 与合并，不自己投影。
//!
//! 消息上的约定（metadata 键、标记、事后补的结果）登记在 [`crate::conventions`] 与 `muliao_runtime::conventions`（工具交回的
//! 文件与图片），这里只读它们来展示。

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;

use agenter::{Content, File, Message, Request, RequestPart, ResponsePart, ToolCall, ToolResult};
use chrono::{DateTime, Utc};
use muliao_platform::text::truncate;
use muliao_platform::tool_views::{ToolRender, View};
use muliao_runtime::conventions::{Attachment, attachment_of_ref, result_images, tool_attachments};
use serde::Serialize;

use crate::answers::{AnsweredQuestion, AskResult, Question, questions_of};
use crate::conventions::{
    check_in_indices, event_indices, finished_at, is_stopped, made_up, parse_event, run_context_index,
};
use crate::error::{Result, broken};
use crate::pending::{ASK_USER, OpenAsks};
use crate::work::{EventSource, parse_source};

/// 各工具的展示规则（`Toolbox::renders`）；只要文字的投影给空的。
pub type Renders = HashMap<String, Arc<ToolRender>>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ItemType {
    User,
    Event,
    Assistant,
    Thinking,
    ToolCall,
    ToolResult,
    ToolError,
    Stopped,
}

/// ask_user 调用的状态，由后端按唯一规则给出（[`annotate_asks`]），前端只渲染。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AskStatus {
    /// 运行还没停下（还不能回答）
    Asking,
    /// 现在能回答（显示表单）
    Pending,
    /// 回答已提交、还排着
    Submitted,
    Answered,
    /// 用户没回答（发了新消息或只回答了别的提问）
    NotAnswered,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct AskView {
    pub status: AskStatus,
    pub questions: Vec<Question>,
    /// answered 时每题的回答
    pub answers: Vec<AnsweredQuestion>,
    /// not_answered 时交给模型的说明
    pub note: Option<String>,
}

/// 停下来等用户批准的工具调用的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalState {
    /// 现在能批准（显示批准/拒绝按钮）
    Pending,
    /// 决定已提交、还排着
    Submitted,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct Item {
    #[serde(rename = "type")]
    pub kind: ItemType,
    pub text: String,
    /// 工具调用、结果与错误的工具名；其他条目为 None
    pub tool_name: Option<String>,
    /// 会话事件（event）的来源，text 是事件内容；其他条目为 None
    pub source: Option<EventSource>,
    /// 事件是运行中检查（工作还在进行）；其他条目为 false
    pub check_in: bool,
    pub tool_call_id: Option<String>,
    /// 来源消息的数据库 id；会话接口填充，用于定位压缩点；实时条目为 None
    pub message_id: Option<i64>,
    /// 用户消息的附件，或工具结果挂上的文件（TOOL_ATTACHMENTS）；其他条目为空
    pub attachments: Vec<Attachment>,
    /// 工具调用开出或续写的 thread（子会话 id），由会话接口按调用 id 填（[`annotate_threads`]）；其他条目为空
    pub threads: Vec<String>,
    /// 工具交回给模型的图片（如浏览器截图）：属于这次调用的过程，不是给用户的文件；其他条目为空
    pub images: Vec<Attachment>,
    /// ask_user 的调用（参数合法时）；其他条目为 None
    pub ask: Option<AskView>,
    /// 停下来等用户批准的工具调用，由会话接口填（[`annotate_approvals`]）；其他条目（含已有结果的调用）为 None
    pub approval: Option<ApprovalState>,
    /// 工具调用的参数、结果按工具自己的展示规则画成的展示块；None 显示原文（工具没定规则、参数或结果不合它的类型、
    /// 被退回的错误、事后补的结果、带图片的结果）。其他条目为 None
    pub view: Option<View>,
    /// 工具调用开始执行的时间（tool_call），工具结束的时间（tool_result、tool_error），或事件交给模型的时间（event，
    /// 所在请求的 timestamp）；其他条目，以及拿不到真实时间的（事后补的结果、没记结束时间的结果、调用方没给保存时间的
    /// 投影）为 None
    pub at: Option<DateTime<Utc>>,
}

impl Item {
    fn new(kind: ItemType, text: impl Into<String>) -> Self {
        Item {
            kind,
            text: text.into(),
            tool_name: None,
            source: None,
            check_in: false,
            tool_call_id: None,
            message_id: None,
            attachments: Vec::new(),
            threads: Vec::new(),
            images: Vec::new(),
            ask: None,
            approval: None,
            view: None,
            at: None,
        }
    }
}

/// 一次工具调用的条目。started_at：调用开始执行的时间（回复在执行工具之前保存，所以历史里是回复那一行的保存时间，运行中
/// 是收到调用事件的时间）。
pub fn call_item(call: &ToolCall, started_at: Option<DateTime<Utc>>, renders: &Renders) -> Item {
    let questions = if call.name == ASK_USER { questions_of(&call.args) } else { Vec::new() };
    Item {
        tool_name: Some(call.name.clone()),
        tool_call_id: Some(call.id.clone()),
        at: started_at,
        // 参数解析不了（不是合法 JSON、不合工具的参数类型）时 call_view 给 None：按原文展示
        view: renders.get(&call.name).and_then(|r| r.call_view(&call.args)),
        ask: (!questions.is_empty()).then(|| AskView {
            status: AskStatus::Asking,
            questions,
            answers: Vec::new(),
            note: None,
        }),
        ..Item::new(ItemType::ToolCall, call.args.clone())
    }
}

/// 结果交给模型的文字（各文字段以换行连接；图片另见 images）。
fn result_text(result: &ToolResult) -> String {
    result
        .content
        .iter()
        .filter_map(|c| match c {
            Content::Text { text } => Some(text.as_str()),
            Content::File(_) => None,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// 工具结果，或失败、参数被拒绝交还模型重试的结果（is_error）。事后补的结果没有结束时间。展示规则只用于工具交回的文本
/// 结果（内容只有一段文字）。
pub fn result_item(result: &ToolResult, renders: &Renders) -> Item {
    let base = Item {
        tool_name: Some(result.tool_name.clone()),
        tool_call_id: Some(result.call_id.clone()),
        ..Item::new(ItemType::ToolResult, result_text(result))
    };
    if result.is_error {
        return Item { kind: ItemType::ToolError, at: finished_at(result), ..base };
    }
    let made_up = made_up(result).is_some();
    let view = match (renders.get(&result.tool_name), result.content.as_slice()) {
        (Some(render), [Content::Text { text }]) if !made_up => render.result_view(&result.tool_name, text),
        _ => None,
    };
    Item {
        attachments: tool_attachments(result),
        images: result_images(result),
        at: if made_up { None } else { finished_at(result) },
        view,
        ..base
    }
}

/// 一条请求里连续的用户内容（文字与附件），合成一条用户条目。
#[derive(Default)]
struct UserContent {
    texts: Vec<String>,
    attachments: Vec<Attachment>,
}

impl UserContent {
    fn flush_into(&mut self, items: &mut Vec<Item>) {
        if self.texts.is_empty() && self.attachments.is_empty() {
            return;
        }
        let own = std::mem::take(self);
        items.push(Item { attachments: own.attachments, ..Item::new(ItemType::User, own.texts.join("\n")) });
    }
}

fn attachment(file: &File) -> Result<Attachment> {
    attachment_of_ref(file).ok_or_else(|| broken(format!("stored user file is not a file-store reference: {file:?}")))
}

/// 一条请求展示成的条目：工具结果各自成条；运行上下文那一段不展示（它是我们加给模型的，不是用户说的话；它仍在消息里
/// 原样重放，这里只是不投影）；会话事件（其中运行中检查另标）各自成条；其余连续的文字与附件合成一条用户消息。
fn request_items(request: &Request, renders: &Renders, items: &mut Vec<Item>) -> Result<()> {
    let events = event_indices(request);
    let check_ins = check_in_indices(request);
    if let Some(i) = check_ins.iter().find(|i| !events.contains(i)) {
        return Err(broken(format!("check-in segment {i} of a request is not an event (events {events:?})")));
    }
    let context = run_context_index(request);
    let mut own = UserContent::default();
    for (i, part) in request.parts.iter().enumerate() {
        if Some(i) == context {
            continue;
        }
        let is_event = events.contains(&i);
        match part {
            RequestPart::ToolResult(r) => {
                own.flush_into(items);
                items.push(result_item(r, renders));
            }
            RequestPart::Text { text } if is_event => {
                own.flush_into(items);
                let (source, content) = parse_event(text)?;
                items.push(Item {
                    source: Some(parse_source(source)),
                    check_in: check_ins.contains(&i),
                    at: Some(request.timestamp),
                    ..Item::new(ItemType::Event, content)
                });
            }
            RequestPart::File(_) if is_event => {
                return Err(broken(format!("event segment {i} of a request is not text")));
            }
            RequestPart::Text { text } => own.texts.push(text.clone()),
            RequestPart::File(f) => own.attachments.push(attachment(f)?),
        }
    }
    own.flush_into(items);
    Ok(())
}

/// saved_at：这些消息保存的时间，即其中工具调用开始执行的时间（见 [`call_item`]）；不需要时间的投影（压缩的文字记录）
/// 不给。存下的消息与约定对不上（事件格式、附件引用）时报数据损坏。
pub fn to_items<'a>(
    messages: impl IntoIterator<Item = &'a Message>,
    renders: &Renders,
    saved_at: Option<DateTime<Utc>>,
) -> Result<Vec<Item>> {
    let mut items = Vec::new();
    for message in messages {
        match message {
            Message::User(request) => request_items(request, renders, &mut items)?,
            Message::Assistant(response) => {
                for part in &response.parts {
                    match part {
                        ResponsePart::Text { text, .. } if !text.is_empty() => {
                            items.push(Item::new(ItemType::Assistant, text.clone()));
                        }
                        ResponsePart::Thinking { text, .. } if !text.is_empty() => {
                            items.push(Item::new(ItemType::Thinking, text.clone()));
                        }
                        ResponsePart::ToolCall(call) => items.push(call_item(call, saved_at, renders)),
                        ResponsePart::Text { .. } | ResponsePart::Thinking { .. } => {}
                    }
                }
            }
        }
        if is_stopped(message) {
            items.push(Item::new(ItemType::Stopped, ""));
        }
    }
    Ok(items)
}

/// 给 ask_user 调用填上状态，唯一规则：
/// - 被退回（结果是 tool_error，如这次运行不提供提问）：不是提问，按普通工具调用加错误展示（ask 为 None）；
/// - 有结果的按结果（[`AskResult`]）：answered / not_answered；
/// - 没有结果的：现在能回答是 pending，回答已提交、还排着是 submitted，否则运行还没停下（asking）。
pub fn annotate_asks(mut items: Vec<Item>, open: &OpenAsks) -> Result<Vec<Item>> {
    let results: HashMap<String, (ItemType, String)> = items
        .iter()
        .filter(|i| {
            i.tool_name.as_deref() == Some(ASK_USER) && matches!(i.kind, ItemType::ToolResult | ItemType::ToolError)
        })
        .filter_map(|i| Some((i.tool_call_id.clone()?, (i.kind, i.text.clone()))))
        .collect();
    for item in &mut items {
        let (Some(ask), Some(id)) = (&mut item.ask, &item.tool_call_id) else { continue };
        match results.get(id) {
            Some((ItemType::ToolError, _)) => item.ask = None,
            Some((_, text)) => {
                let parsed: AskResult = serde_json::from_str(text)
                    .map_err(|e| broken(format!("result of ask_user call {id} is not an ask result: {e}")))?;
                ask.status = if parsed.answered { AskStatus::Answered } else { AskStatus::NotAnswered };
                ask.answers = parsed.answers;
                ask.note = parsed.note;
            }
            None if open.question(id).is_some() => ask.status = AskStatus::Pending,
            None if open.submitted.contains(id) => ask.status = AskStatus::Submitted,
            None => {}
        }
    }
    Ok(items)
}

/// 给等批准的工具调用填上状态：现在能批准是 pending，决定已提交、还排着是 submitted。两者都只含还没有结果的调用
/// （[`crate::pending::open_asks_of`]），所以不看结果条目。
pub fn annotate_approvals(mut items: Vec<Item>, open: &OpenAsks) -> Vec<Item> {
    for item in &mut items {
        let (ItemType::ToolCall, Some(id)) = (item.kind, &item.tool_call_id) else { continue };
        if open.can_approve(id) {
            item.approval = Some(ApprovalState::Pending);
        } else if open.approvals_submitted.contains(id) {
            item.approval = Some(ApprovalState::Submitted);
        }
    }
    items
}

/// 工具调用挂上它开出或续写的 thread（call_threads：调用 id → thread，见 [`crate::thread_calls::call_threads_in`]），界面
/// 据此把 thread 显示在这次调用处。只看调用 id，不看调用有没有结果、结果是谁补的。
pub fn annotate_threads(mut items: Vec<Item>, call_threads: &BTreeMap<String, String>) -> Vec<Item> {
    for item in &mut items {
        if item.kind == ItemType::ToolCall
            && let Some(thread) = item.tool_call_id.as_ref().and_then(|id| call_threads.get(id))
        {
            item.threads = vec![thread.clone()];
        }
    }
    items
}

/// 条目的文字记录（压缩交给模型总结的原文）：思考不进，每条截到 max_item_chars 个字符。
pub fn to_text(items: &[Item], max_item_chars: usize) -> String {
    let mut lines = Vec::new();
    for item in items {
        let label = match item.kind {
            ItemType::User => "USER",
            ItemType::Event => "EVENT",
            ItemType::Assistant => "ASSISTANT",
            ItemType::Thinking => continue,
            ItemType::ToolCall => "TOOL CALL",
            ItemType::ToolResult => "TOOL RESULT",
            ItemType::ToolError => "TOOL ERROR",
            ItemType::Stopped => "STOPPED BY USER",
        };
        let named = match &item.source {
            Some(EventSource::Thread { raw, .. } | EventSource::Job { raw, .. } | EventSource::Other { raw }) => {
                Some(raw.as_str())
            }
            None => item.tool_name.as_deref(),
        };
        let name = named.filter(|n| !n.is_empty()).map(|n| format!(" {n}")).unwrap_or_default();
        let files: String =
            item.attachments.iter().map(|a| format!(" [attachment '{}' ({})]", a.name, a.media_type)).collect();
        lines.push(format!("[{label}{name}] {}{files}", truncate(&item.text, max_item_chars)));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;

    use agenter::{FileSource, Response};
    use muliao_platform::tool_views::{ViewBlock, fields};
    use serde::Deserialize;

    use super::*;
    use crate::answers::QuestionOption;
    use crate::conventions::{
        MadeUp, attach_run_context, format_event, made_up_result, mark_events, mark_stopped, set_finished_at,
    };

    fn at(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    fn call(id: &str, name: &str, args: &str) -> ToolCall {
        ToolCall { id: id.into(), name: name.into(), args: args.into(), meta: None }
    }

    fn result(id: &str, name: &str, content: Vec<Content>, is_error: bool) -> ToolResult {
        ToolResult { call_id: id.into(), tool_name: name.into(), content, is_error, metadata: None }
    }

    fn response(parts: Vec<ResponsePart>) -> Message {
        Message::Assistant(Response {
            parts,
            protocol: "anthropic".into(),
            model: "m".into(),
            response_id: None,
            finish_reason: None,
            usage: Default::default(),
            timestamp: Utc::now(),
            metadata: None,
        })
    }

    fn request(parts: Vec<RequestPart>) -> Request {
        Request { parts, timestamp: at("2026-11-12T08:00:00Z"), metadata: None }
    }

    fn file(url: &str) -> File {
        File { media_type: "image/png".into(), source: FileSource::Url(url.into()) }
    }

    #[derive(Deserialize)]
    struct Args {
        q: String,
    }

    fn renders() -> Renders {
        let r = ToolRender::new()
            .call(|a: &Args| Some(vec![fields([("q", a.q.as_str().into())])]))
            .result_text(|t| Some(vec![ViewBlock::Markdown { title: None, text: t.to_owned() }]));
        HashMap::from([("t".to_owned(), Arc::new(r))])
    }

    fn kinds(items: &[Item]) -> Vec<ItemType> {
        items.iter().map(|i| i.kind).collect()
    }

    #[test]
    fn request_splits_events_check_ins_user_content_and_hides_run_context() {
        let mut r = request(vec![
            RequestPart::ToolResult(result("c1", "t", vec![Content::text("ok")], false)),
            RequestPart::Text { text: "hi".into() },
            RequestPart::File(file("/api/files/abc/pic.png")),
            RequestPart::Text { text: format_event("job:7", "done") },
            RequestPart::Text { text: format_event("thread:s1", "still going") },
            RequestPart::Text { text: "tail".into() },
        ]);
        mark_events(&mut r, &[3, 4], &[4]);
        attach_run_context(&mut r, "now is 10:00");
        let items = to_items([&Message::User(r)], &Renders::new(), None).unwrap();
        use ItemType::*;
        assert_eq!(kinds(&items), [ToolResult, User, Event, Event, User]);
        assert_eq!(items[1].text, "hi");
        assert_eq!(items[1].attachments.len(), 1);
        assert_eq!(items[1].attachments[0].name, "pic.png");
        assert!(items[1].attachments[0].inline);
        assert_eq!(items[2].text, "done");
        assert_eq!(items[2].source, Some(EventSource::Job { raw: "job:7".into(), job_id: 7 }));
        assert!(!items[2].check_in);
        assert_eq!(items[2].at, Some(at("2026-11-12T08:00:00Z")));
        assert!(items[3].check_in);
        assert_eq!(items[3].source, Some(EventSource::Thread { raw: "thread:s1".into(), session_id: "s1".into() }));
        assert_eq!(items[4].text, "tail");
        assert!(items.iter().all(|i| !i.text.contains("now is")));
    }

    #[test]
    fn broken_requests_are_reported() {
        let mut r = request(vec![RequestPart::Text { text: "not an event".into() }]);
        mark_events(&mut r, &[0], &[]);
        assert!(to_items([&Message::User(r)], &Renders::new(), None).is_err());
        let r = request(vec![RequestPart::File(file("https://elsewhere/x.png"))]);
        assert!(to_items([&Message::User(r)], &Renders::new(), None).is_err());
    }

    #[test]
    fn response_parts_calls_and_stop_mark() {
        let saved = at("2026-11-12T09:00:00Z");
        let mut m = response(vec![
            ResponsePart::Thinking { text: String::new(), meta: None },
            ResponsePart::Thinking { text: "hmm".into(), meta: None },
            ResponsePart::Text { text: String::new(), meta: None },
            ResponsePart::Text { text: "answer".into(), meta: None },
            ResponsePart::ToolCall(call("c1", "t", "{\"q\":\"x\"}")),
            ResponsePart::ToolCall(call("c2", "t", "{not json")),
        ]);
        mark_stopped(&mut m);
        let items = to_items([&m], &renders(), Some(saved)).unwrap();
        use ItemType::*;
        assert_eq!(kinds(&items), [Thinking, Assistant, ToolCall, ToolCall, Stopped]);
        assert_eq!(items[2].at, Some(saved));
        assert_eq!(items[2].view, Some(vec![fields([("q", "x".into())])]));
        assert_eq!(items[3].view, None);
        assert_eq!(items[3].text, "{not json");
    }

    #[test]
    fn results_errors_and_made_up_results() {
        let ended = at("2026-11-12T09:00:05Z");
        let mut ok = result("c1", "t", vec![Content::text("out")], false);
        set_finished_at(&mut ok, ended);
        let mut err = result("c2", "t", vec![Content::text("bad args")], true);
        set_finished_at(&mut err, ended);
        let made = made_up_result(&call("c3", "t", "{}"), MadeUp::Stopped);
        let with_image =
            result("c4", "t", vec![Content::text("shot"), Content::File(file("/api/files/i/s.png"))], false);
        let r = renders();

        let ok = result_item(&ok, &r);
        assert_eq!((ok.kind, ok.at, ok.text.as_str()), (ItemType::ToolResult, Some(ended), "out"));
        assert!(ok.view.is_some());

        let err = result_item(&err, &r);
        assert_eq!((err.kind, err.at, err.view.clone()), (ItemType::ToolError, Some(ended), None));

        let made = result_item(&made, &r);
        assert_eq!((made.kind, made.at, made.view.clone()), (ItemType::ToolResult, None, None));

        let with_image = result_item(&with_image, &r);
        assert_eq!(with_image.view, None);
        assert_eq!(with_image.images.len(), 1);
        assert_eq!(with_image.text, "shot");
    }

    fn ask_args() -> String {
        serde_json::json!({"questions": [{"question": "Which?", "header": "Pick", "options": [
            {"label": "a"}, {"label": "b"}]}]})
        .to_string()
    }

    fn ask_result(answered: bool) -> String {
        serde_json::to_string(&AskResult {
            answered,
            answers: if answered {
                vec![AnsweredQuestion { question: "Which?".into(), selected: vec!["a".into()], other: None }]
            } else {
                vec![]
            },
            note: (!answered).then(|| "skipped".into()),
        })
        .unwrap()
    }

    #[test]
    fn ask_states() {
        let args = ask_args();
        let calls = response(
            ["asking", "pending", "submitted", "answered", "not", "refused"]
                .map(|id| ResponsePart::ToolCall(call(id, ASK_USER, &args)))
                .into_iter()
                .collect(),
        );
        let results = Message::User(request(vec![
            RequestPart::ToolResult(result("answered", ASK_USER, vec![Content::text(ask_result(true))], false)),
            RequestPart::ToolResult(result("not", ASK_USER, vec![Content::text(ask_result(false))], false)),
            RequestPart::ToolResult(result("refused", ASK_USER, vec![Content::text("no asking here")], true)),
        ]));
        let items = to_items([&calls, &results], &Renders::new(), None).unwrap();
        assert!(items.iter().filter(|i| i.kind == ItemType::ToolCall).all(|i| i.ask.is_some()));
        let open = OpenAsks {
            answerable: vec![("pending".into(), vec![])],
            submitted: HashSet::from(["submitted".to_owned()]),
            ..Default::default()
        };
        let items = annotate_asks(items, &open).unwrap();
        let status = |id: &str| {
            items
                .iter()
                .find(|i| i.kind == ItemType::ToolCall && i.tool_call_id.as_deref() == Some(id))
                .unwrap()
                .ask
                .as_ref()
                .map(|a| a.status)
        };
        assert_eq!(status("asking"), Some(AskStatus::Asking));
        assert_eq!(status("pending"), Some(AskStatus::Pending));
        assert_eq!(status("submitted"), Some(AskStatus::Submitted));
        assert_eq!(status("answered"), Some(AskStatus::Answered));
        assert_eq!(status("not"), Some(AskStatus::NotAnswered));
        assert_eq!(status("refused"), None);
        let answered = items.iter().find(|i| i.tool_call_id.as_deref() == Some("answered")).unwrap();
        assert_eq!(answered.ask.as_ref().unwrap().answers[0].selected, ["a"]);
        let not = items.iter().find(|i| i.tool_call_id.as_deref() == Some("not")).unwrap();
        assert_eq!(not.ask.as_ref().unwrap().note.as_deref(), Some("skipped"));
        assert_eq!(
            Some(&QuestionOption { label: "a".into(), description: String::new() }),
            not.ask.as_ref().unwrap().questions[0].options.first()
        );

        let bad = to_items([&response(vec![ResponsePart::ToolCall(call("x", ASK_USER, "{}"))])], &Renders::new(), None)
            .unwrap();
        assert!(bad[0].ask.is_none());
    }

    #[test]
    fn approvals_and_threads() {
        let m = response(vec![
            ResponsePart::ToolCall(call("p", "t", "{}")),
            ResponsePart::ToolCall(call("s", "t", "{}")),
            ResponsePart::ToolCall(call("n", "t", "{}")),
        ]);
        let items = to_items([&m], &Renders::new(), None).unwrap();
        let open = OpenAsks {
            approvable: vec![("p".into(), "t".into())],
            submitted: HashSet::from(["s".to_owned()]),
            approvals_submitted: HashSet::from(["s".to_owned()]),
            ..Default::default()
        };
        let items = annotate_approvals(items, &open);
        assert_eq!(
            items.iter().map(|i| i.approval).collect::<Vec<_>>(),
            [Some(ApprovalState::Pending), Some(ApprovalState::Submitted), None]
        );
        let items = annotate_threads(items, &BTreeMap::from([("n".to_owned(), "child".to_owned())]));
        assert_eq!(items.iter().map(|i| i.threads.len()).collect::<Vec<_>>(), [0, 0, 1]);
        assert_eq!(items[2].threads, ["child"]);
    }

    #[test]
    fn text_of_items() {
        let r = request(vec![
            RequestPart::Text { text: "look".into() },
            RequestPart::File(file("/api/files/abc/pic.png")),
            RequestPart::Text { text: format_event("job:7", "done") },
        ]);
        let mut r = r;
        mark_events(&mut r, &[2], &[]);
        let m = response(vec![
            ResponsePart::Thinking { text: "secret thought".into(), meta: None },
            ResponsePart::ToolCall(call("c1", "t", "{\"q\":\"0123456789\"}")),
        ]);
        let items = to_items([&Message::User(r), &m], &Renders::new(), None).unwrap();
        assert_eq!(
            to_text(&items, 8),
            "[USER] look [attachment 'pic.png' (image/png)]\n[EVENT job:7] done\n\
             [TOOL CALL t] {\"q\":\"01…[truncated: 10 more chars]"
        );
    }
}
