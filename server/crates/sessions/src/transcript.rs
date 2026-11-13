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
use muliao_platform::tool_views::{ToolRender, View, truncated};
use muliao_runtime::conventions::{Attachment, attachment_of_ref, result_images, tool_attachments};
use serde::Serialize;

use crate::answers::Waiting;
use crate::answers::{AnsweredQuestion, AskResult, Question, questions_of};
use crate::conventions::{PartKind, event_content, finished_at, is_stopped, made_up, part_marks, run_context_index};
use crate::origins::Origin;
use crate::pending::{ASK_USER, OpenAsk, OpenAsks};
use crate::work::EventSource;
use muliao_platform::errors::{Result, broken};

/// 各工具的展示规则（`Toolbox::renders`）；只要文字的投影给空的。
pub type Renders = HashMap<String, Arc<ToolRender>>;

/// ask_user 调用的状态，由后端按唯一规则给出（[`annotate_asks`]），前端只渲染。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum AskState {
    /// 运行还没停下（还不能回答）
    Asking,
    /// 现在能回答（显示表单）
    Pending,
    /// 回答已提交、还排着
    Submitted,
    /// 每题的回答
    Answered { answers: Vec<AnsweredQuestion> },
    /// 用户没回答（发了新消息或只回答了别的提问）；note 是交给模型的说明
    NotAnswered { note: Option<String> },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct AskView {
    pub questions: Vec<Question>,
    pub state: AskState,
}

/// 停下来等用户批准的工具调用的状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ApprovalState {
    /// 现在能批准（显示批准/拒绝按钮）
    Pending,
    /// 决定已提交、还排着
    Submitted,
}

/// 工具调用要用户操作的那一面：提问（ask_user，参数合法时），或停下来等批准（由会话接口填，[`annotate_approvals`]）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CallWait {
    Ask(AskView),
    Approval { state: ApprovalState },
}

#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct UserItem {
    pub text: String,
    pub attachments: Vec<Attachment>,
}

/// 会话事件：text 是事件内容。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct EventItem {
    pub text: String,
    pub source: EventSource,
    /// 运行中检查（工作还在进行）
    pub check_in: bool,
    /// 交给模型的时间（所在请求的 timestamp）
    pub at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct CallItem {
    pub tool_name: String,
    pub tool_call_id: String,
    /// 调用参数原文
    pub text: String,
    /// 开出或续写的 thread（子会话 id），由会话接口按调用 id 填（[`annotate_threads`]）
    pub threads: Vec<String>,
    pub wait: Option<CallWait>,
    /// 参数按工具自己的展示规则画成的展示块；None 显示原文（工具没定规则、参数不合它的类型）
    #[schema(value_type = Option<Vec<muliao_platform::tool_views::ViewBlock>>)]
    pub view: Option<View>,
    /// 开始执行的时间；调用方没给保存时间的投影为 None
    pub at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct ResultItem {
    pub tool_name: String,
    pub tool_call_id: String,
    /// 交给模型的文字（各文字段以换行连接）
    pub text: String,
    /// 工具结果挂上的文件（TOOL_ATTACHMENTS）
    pub attachments: Vec<Attachment>,
    /// 工具交回给模型的图片（如浏览器截图）：属于这次调用的过程，不是给用户的文件
    pub images: Vec<Attachment>,
    /// 结果按工具自己的展示规则画成的展示块；None 显示原文（工具没定规则、结果不合它的类型、事后补的结果、带图片的结果）
    #[schema(value_type = Option<Vec<muliao_platform::tool_views::ViewBlock>>)]
    pub view: Option<View>,
    /// 工具结束的时间；事后补的结果、没记结束时间的结果为 None
    pub at: Option<DateTime<Utc>>,
}

/// 失败、参数被拒绝交还模型重试的结果。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct ErrorItem {
    pub tool_name: String,
    pub tool_call_id: String,
    pub text: String,
    pub at: Option<DateTime<Utc>>,
}

/// 一条展示条目：每种只带自己的字段。U 是用户消息条目：会话页是 [`UserItem`]，热历史的时间线另带发送方
/// （[`crate::recall::SentUser`]）。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Item<U = UserItem> {
    User(#[schema(inline)] U),
    Event(EventItem),
    Assistant {
        text: String,
    },
    Thinking {
        text: String,
    },
    ToolCall(CallItem),
    ToolResult(ResultItem),
    ToolError(ErrorItem),
    /// 用户中途停下了运行
    Stopped,
}

/// 历史里的一条：来源消息的数据库 id（用于定位压缩点）与条目。
#[derive(Debug, Clone, PartialEq, Serialize, utoipa::ToSchema)]
pub struct SavedItem {
    pub message_id: i64,
    pub item: Item,
}

impl<U: AsRef<UserItem>> Item<U> {
    /// 条目的正文（工具调用是参数原文）；停下的标记没有。
    pub fn text(&self) -> &str {
        match self {
            Item::User(u) => &u.as_ref().text,
            Item::Event(EventItem { text, .. })
            | Item::Assistant { text }
            | Item::Thinking { text }
            | Item::ToolCall(CallItem { text, .. })
            | Item::ToolResult(ResultItem { text, .. })
            | Item::ToolError(ErrorItem { text, .. }) => text,
            Item::Stopped => "",
        }
    }

    /// 给用户的文件：用户消息的附件，或工具结果挂上的文件。
    pub fn attachments(&self) -> &[Attachment] {
        match self {
            Item::User(u) => &u.as_ref().attachments,
            Item::ToolResult(r) => &r.attachments,
            _ => &[],
        }
    }
}

impl AsRef<UserItem> for UserItem {
    fn as_ref(&self) -> &UserItem {
        self
    }
}

impl Item {
    /// 用户消息条目换成 f 给出的（其余原样）。
    pub fn map_user<V>(self, f: impl FnOnce(UserItem) -> V) -> Item<V> {
        match self {
            Item::User(u) => Item::User(f(u)),
            Item::Event(e) => Item::Event(e),
            Item::Assistant { text } => Item::Assistant { text },
            Item::Thinking { text } => Item::Thinking { text },
            Item::ToolCall(c) => Item::ToolCall(c),
            Item::ToolResult(r) => Item::ToolResult(r),
            Item::ToolError(e) => Item::ToolError(e),
            Item::Stopped => Item::Stopped,
        }
    }

    /// 实时展示的预览：正文与展示块截到 max_chars 个字符。
    pub(crate) fn preview(mut self, max_chars: usize) -> Self {
        match &mut self {
            Item::ToolCall(CallItem { text, view, .. }) | Item::ToolResult(ResultItem { text, view, .. }) => {
                *text = truncate(text, max_chars);
                *view = view.take().map(|v| truncated(v, max_chars));
            }
            Item::User(UserItem { text, .. })
            | Item::Event(EventItem { text, .. })
            | Item::Assistant { text }
            | Item::Thinking { text }
            | Item::ToolError(ErrorItem { text, .. }) => *text = truncate(text, max_chars),
            Item::Stopped => {}
        }
        self
    }
}

/// 一次工具调用的条目。started_at：调用开始执行的时间（回复在执行工具之前保存，所以历史里是回复那一行的保存时间，运行中
/// 是收到调用事件的时间）。
pub fn call_item(call: &ToolCall, started_at: Option<DateTime<Utc>>, renders: &Renders) -> Item {
    call_entry(call, started_at, renders)
}

fn call_entry<U>(call: &ToolCall, started_at: Option<DateTime<Utc>>, renders: &Renders) -> Item<U> {
    let questions = if call.name == ASK_USER { questions_of(&call.args) } else { Vec::new() };
    Item::ToolCall(CallItem {
        tool_name: call.name.clone(),
        tool_call_id: call.id.clone(),
        text: call.args.clone(),
        threads: Vec::new(),
        wait: (!questions.is_empty()).then_some(CallWait::Ask(AskView { questions, state: AskState::Asking })),
        // 参数解析不了（不是合法 JSON、不合工具的参数类型）时 call_view 给 None：按原文展示
        view: renders.get(&call.name).and_then(|r| r.call_view(&call.args)),
        at: started_at,
    })
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
/// 结果（内容只有一段文字）。挂上的文件记录形状不对时报数据损坏。
pub fn result_item(result: &ToolResult, renders: &Renders) -> Result<Item> {
    result_entry(result, renders)
}

fn result_entry<U>(result: &ToolResult, renders: &Renders) -> Result<Item<U>> {
    let (tool_name, tool_call_id, text) = (result.tool_name.clone(), result.call_id.clone(), result_text(result));
    if result.is_error {
        return Ok(Item::ToolError(ErrorItem { tool_name, tool_call_id, text, at: finished_at(result) }));
    }
    let attachments = tool_attachments(result).map_err(broken)?.iter().map(Attachment::from).collect();
    let made_up = made_up(result).is_some();
    let view = match (renders.get(&result.tool_name), result.content.as_slice()) {
        (Some(render), [Content::Text { text }]) if !made_up => render.result_view(&result.tool_name, text),
        _ => None,
    };
    Ok(Item::ToolResult(ResultItem {
        tool_name,
        tool_call_id,
        text,
        attachments,
        images: result_images(result),
        view,
        at: if made_up { None } else { finished_at(result) },
    }))
}

/// 一条请求里连续的、同一发送方的用户内容（文字与附件），合成一条用户条目。sender：信件记下的发送方，没记的（迟到的
/// 回答、记下发送方之前存的消息）为 None。
#[derive(Default)]
struct UserContent {
    texts: Vec<String>,
    attachments: Vec<Attachment>,
    sender: Option<Origin>,
}

impl UserContent {
    fn is_empty(&self) -> bool {
        self.texts.is_empty() && self.attachments.is_empty()
    }

    /// 接下来的一段来自 sender：发送方变了先把已有的合成一条。
    fn from<U>(
        &mut self,
        sender: Option<Origin>,
        items: &mut Vec<Item<U>>,
        user: &impl Fn(UserItem, Option<Origin>) -> U,
    ) {
        if self.sender != sender {
            self.flush_into(items, user);
            self.sender = sender;
        }
    }

    fn flush_into<U>(&mut self, items: &mut Vec<Item<U>>, user: &impl Fn(UserItem, Option<Origin>) -> U) {
        if self.is_empty() {
            return;
        }
        let own = std::mem::take(self);
        items.push(Item::User(user(UserItem { text: own.texts.join("\n"), attachments: own.attachments }, own.sender)));
        self.sender = own.sender;
    }
}

fn attachment(file: &File) -> Result<Attachment> {
    attachment_of_ref(file).ok_or_else(|| broken(format!("stored user file is not a file-store reference: {file:?}")))
}

/// 一条请求展示成的条目：工具结果各自成条；运行上下文那一段不展示（它是我们加给模型的，不是用户说的话；它仍在消息里
/// 原样重放，这里只是不投影）；会话事件（其中运行中检查另标）各自成条；其余连续的、同一发送方的文字与附件合成一条用户
/// 消息。
fn request_items<U>(
    request: &Request,
    renders: &Renders,
    items: &mut Vec<Item<U>>,
    user: &impl Fn(UserItem, Option<Origin>) -> U,
) -> Result<()> {
    let marks = part_marks(request)?;
    let context = run_context_index(request);
    let mut own = UserContent::default();
    for (i, part) in request.parts.iter().enumerate() {
        if Some(i) == context {
            continue;
        }
        let kind = marks.iter().find(|m| m.part == i).map(|m| &m.kind);
        let sender = match kind {
            Some(PartKind::Message { origin }) => Some(*origin),
            _ => None,
        };
        match (part, kind) {
            (RequestPart::ToolResult(r), _) => {
                own.flush_into(items, user);
                items.push(result_entry(r, renders)?);
            }
            (RequestPart::Text { text }, Some(PartKind::Event { source, check_in })) => {
                own.flush_into(items, user);
                items.push(Item::Event(EventItem {
                    text: event_content(text, source).to_owned(),
                    source: source.clone(),
                    check_in: *check_in,
                    at: request.timestamp,
                }));
            }
            (RequestPart::Text { text }, _) => {
                own.from(sender, items, user);
                own.texts.push(text.clone());
            }
            (RequestPart::File(f), _) => {
                own.from(sender, items, user);
                own.attachments.push(attachment(f)?);
            }
        }
    }
    own.flush_into(items, user);
    Ok(())
}

/// saved_at：这些消息保存的时间，即其中工具调用开始执行的时间（见 [`call_item`]）；不需要时间的投影（压缩的文字记录）
/// 不给。存下的消息与约定对不上（各段的记录、附件引用）时报数据损坏。
pub fn to_items<'a>(
    messages: impl IntoIterator<Item = &'a Message>,
    renders: &Renders,
    saved_at: Option<DateTime<Utc>>,
) -> Result<Vec<Item>> {
    to_items_with(messages, renders, saved_at, |u, _| u)
}

/// 同 [`to_items`]，用户消息条目由 user 按条目与它记下的发送方（没记的为 None）给出。
pub fn to_items_with<'a, U>(
    messages: impl IntoIterator<Item = &'a Message>,
    renders: &Renders,
    saved_at: Option<DateTime<Utc>>,
    user: impl Fn(UserItem, Option<Origin>) -> U,
) -> Result<Vec<Item<U>>> {
    let mut items = Vec::new();
    for message in messages {
        match message {
            Message::User(request) => request_items(request, renders, &mut items, &user)?,
            Message::Assistant(response) => {
                for part in &response.parts {
                    match part {
                        ResponsePart::Text { text, .. } if !text.is_empty() => {
                            items.push(Item::Assistant { text: text.clone() });
                        }
                        ResponsePart::Thinking { text, .. } if !text.is_empty() => {
                            items.push(Item::Thinking { text: text.clone() });
                        }
                        ResponsePart::ToolCall(call) => items.push(call_entry(call, saved_at, renders)),
                        ResponsePart::Text { .. } | ResponsePart::Thinking { .. } => {}
                    }
                }
            }
        }
        if is_stopped(message) {
            items.push(Item::Stopped);
        }
    }
    Ok(items)
}

/// 给 ask_user 调用填上状态，唯一规则：
/// - 被退回（结果是 tool_error，如这次运行不提供提问）：不是提问，按普通工具调用加错误展示（wait 为 None）；
/// - 有结果的按结果（[`AskResult`]）：answered / not_answered；
/// - 没有结果的：现在能回答是 pending，回答已提交、还排着是 submitted，否则运行还没停下（asking）。
pub fn annotate_asks(items: &mut [Item], open: &OpenAsks) -> Result<()> {
    let results: HashMap<String, Option<String>> = items
        .iter()
        .filter_map(|i| match i {
            Item::ToolResult(r) if r.tool_name == ASK_USER => Some((r.tool_call_id.clone(), Some(r.text.clone()))),
            Item::ToolError(e) if e.tool_name == ASK_USER => Some((e.tool_call_id.clone(), None)),
            _ => None,
        })
        .collect();
    for item in items.iter_mut() {
        let Item::ToolCall(call) = item else { continue };
        let Some(CallWait::Ask(ask)) = &mut call.wait else { continue };
        let id = &call.tool_call_id;
        match results.get(id) {
            Some(None) => call.wait = None,
            Some(Some(text)) => {
                let parsed: AskResult = serde_json::from_str(text)
                    .map_err(|e| broken(format!("result of ask_user call {id} is not an ask result: {e}")))?;
                ask.state = if parsed.answered {
                    AskState::Answered { answers: parsed.answers }
                } else {
                    AskState::NotAnswered { note: parsed.note }
                };
            }
            None => match open.get(id) {
                Some(OpenAsk::Question(_)) => ask.state = AskState::Pending,
                Some(OpenAsk::Submitted(_)) => ask.state = AskState::Submitted,
                Some(OpenAsk::Approval(_)) | None => {}
            },
        }
    }
    Ok(())
}

/// 给等批准的工具调用填上状态：现在能批准是 pending，决定已提交、还排着是 submitted。两者都只含还没有结果的调用
/// （[`crate::pending::open_asks_of`]），所以不看结果条目。
pub fn annotate_approvals(items: &mut [Item], open: &OpenAsks) {
    for item in items.iter_mut() {
        let Item::ToolCall(call) = item else { continue };
        let id = &call.tool_call_id;
        let state = match open.get(id) {
            Some(OpenAsk::Approval(_)) => ApprovalState::Pending,
            Some(OpenAsk::Submitted(Waiting::Approval)) => ApprovalState::Submitted,
            _ => continue,
        };
        call.wait = Some(CallWait::Approval { state });
    }
}

/// 工具调用挂上它开出或续写的 thread（call_threads：调用 id → thread，见 [`crate::thread_calls::call_threads_in`]），界面
/// 据此把 thread 显示在这次调用处。只看调用 id，不看调用有没有结果、结果是谁补的。
pub fn annotate_threads(items: &mut [Item], call_threads: &BTreeMap<String, String>) {
    for item in items.iter_mut() {
        if let Item::ToolCall(call) = item
            && let Some(thread) = call_threads.get(&call.tool_call_id)
        {
            call.threads = vec![thread.clone()];
        }
    }
}

/// 条目的文字记录（压缩交给模型总结的原文）：思考不进，每条截到 max_item_chars 个字符。
pub fn to_text(items: &[Item], max_item_chars: usize) -> String {
    let mut lines = Vec::new();
    for item in items {
        let (label, name) = match item {
            Item::User(_) => ("USER", None),
            Item::Event(e) => ("EVENT", Some(e.source.raw())),
            Item::Assistant { .. } => ("ASSISTANT", None),
            Item::Thinking { .. } => continue,
            Item::ToolCall(c) => ("TOOL CALL", Some(c.tool_name.clone())),
            Item::ToolResult(r) => ("TOOL RESULT", Some(r.tool_name.clone())),
            Item::ToolError(e) => ("TOOL ERROR", Some(e.tool_name.clone())),
            Item::Stopped => ("STOPPED BY USER", None),
        };
        let name = name.filter(|n| !n.is_empty()).map(|n| format!(" {n}")).unwrap_or_default();
        let files: String =
            item.attachments().iter().map(|a| format!(" [attachment '{}' ({})]", a.name, a.media_type)).collect();
        lines.push(format!("[{label}{name}] {}{files}", truncate(item.text(), max_item_chars)));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use agenter::{FileSource, Response};
    use muliao_platform::tool_views::{ViewBlock, fields};
    use serde::Deserialize;

    use super::*;
    use crate::answers::QuestionOption;
    use crate::conventions::{
        MadeUp, PartMark, attach_run_context, format_event, made_up_result, mark_parts, mark_stopped, set_finished_at,
    };

    fn event(part: usize, source: EventSource, check_in: bool) -> PartMark {
        PartMark { part, kind: PartKind::Event { source, check_in } }
    }

    fn message(part: usize, origin: Origin) -> PartMark {
        PartMark { part, kind: PartKind::Message { origin } }
    }

    fn job7() -> EventSource {
        EventSource::Job { job_id: 7 }
    }

    fn thread_s1() -> EventSource {
        EventSource::Thread { session_id: "s1".into() }
    }

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

    fn kinds(items: &[Item]) -> Vec<String> {
        items.iter().map(|i| serde_json::to_value(i).unwrap()["type"].as_str().unwrap().to_owned()).collect()
    }

    fn call_of(items: &[Item], id: &str) -> CallItem {
        items
            .iter()
            .find_map(|i| match i {
                Item::ToolCall(c) if c.tool_call_id == id => Some(c.clone()),
                _ => None,
            })
            .unwrap()
    }

    #[test]
    fn request_splits_events_check_ins_user_content_and_hides_run_context() {
        let mut r = request(vec![
            RequestPart::ToolResult(result("c1", "t", vec![Content::text("ok")], false)),
            RequestPart::Text { text: "hi".into() },
            RequestPart::File(file("/api/files/abc/pic.png")),
            RequestPart::Text { text: format_event(&job7(), "done") },
            RequestPart::Text { text: format_event(&thread_s1(), "still going") },
            RequestPart::Text { text: "tail".into() },
            RequestPart::Text { text: "late".into() },
        ]);
        mark_parts(
            &mut r,
            vec![
                message(1, Origin::User),
                message(2, Origin::User),
                event(3, job7(), false),
                event(4, thread_s1(), true),
                message(5, Origin::Trigger),
            ],
        )
        .unwrap();
        attach_run_context(&mut r, "now is 10:00").unwrap();
        let senders: Vec<Option<Origin>> = to_items_with([&Message::User(r.clone())], &Renders::new(), None, |_, s| s)
            .unwrap()
            .into_iter()
            .filter_map(|i| if let Item::User(s) = i { Some(s) } else { None })
            .collect();
        assert_eq!(senders, [Some(Origin::User), Some(Origin::Trigger), None]);
        let items = to_items([&Message::User(r)], &Renders::new(), None).unwrap();
        assert_eq!(kinds(&items), ["tool_result", "user", "event", "event", "user", "user"]);
        let Item::User(user) = &items[1] else { panic!() };
        assert_eq!(user.text, "hi");
        assert_eq!(user.attachments.len(), 1);
        assert_eq!(user.attachments[0].name, "pic.png");
        assert!(user.attachments[0].inline);
        let ts = at("2026-11-12T08:00:00Z");
        assert_eq!(
            items[2],
            Item::Event(EventItem {
                text: "done".into(),
                source: EventSource::Job { job_id: 7 },
                check_in: false,
                at: ts
            })
        );
        assert_eq!(
            items[3],
            Item::Event(EventItem {
                text: "still going".into(),
                source: EventSource::Thread { session_id: "s1".into() },
                check_in: true,
                at: ts
            })
        );
        assert_eq!(items[4].text(), "tail");
        assert!(items.iter().all(|i| !i.text().contains("now is")));
    }

    #[test]
    fn broken_requests_are_reported() {
        let mut r = request(vec![RequestPart::Text { text: "x".into() }]);
        r.metadata = Some(serde_json::json!({ crate::conventions::PARTS: [{ "part": 0, "event": "nope" }] }));
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
        assert_eq!(kinds(&items), ["thinking", "assistant", "tool_call", "tool_call", "stopped"]);
        let c1 = call_of(&items, "c1");
        assert_eq!(c1.at, Some(saved));
        assert_eq!(c1.view, Some(vec![fields([("q", "x".into())])]));
        let c2 = call_of(&items, "c2");
        assert_eq!(c2.view, None);
        assert_eq!(c2.text, "{not json");
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

        let Item::ToolResult(ok) = result_item(&ok, &r).unwrap() else { panic!() };
        assert_eq!((ok.at, ok.text.as_str()), (Some(ended), "out"));
        assert!(ok.view.is_some());

        assert_eq!(
            result_item(&err, &r).unwrap(),
            Item::ToolError(ErrorItem {
                tool_name: "t".into(),
                tool_call_id: "c2".into(),
                text: "bad args".into(),
                at: Some(ended)
            })
        );

        let Item::ToolResult(made) = result_item(&made, &r).unwrap() else { panic!() };
        assert_eq!((made.at, made.view), (None, None));

        let Item::ToolResult(with_image) = result_item(&with_image, &r).unwrap() else { panic!() };
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
            answers: if answered { answered_a() } else { vec![] },
            note: (!answered).then(|| "skipped".into()),
        })
        .unwrap()
    }

    fn answered_a() -> Vec<AnsweredQuestion> {
        vec![AnsweredQuestion { question: "Which?".into(), selected: vec!["a".into()], other: None }]
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
        let mut items = to_items([&calls, &results], &Renders::new(), None).unwrap();
        assert!(items.iter().all(|i| !matches!(i, Item::ToolCall(c) if c.wait.is_none())));
        let open = OpenAsks(vec![
            ("pending".into(), OpenAsk::Question(vec![])),
            ("submitted".into(), OpenAsk::Submitted(Waiting::AskUser)),
        ]);
        annotate_asks(&mut items, &open).unwrap();
        let state = |id: &str| match call_of(&items, id).wait {
            Some(CallWait::Ask(a)) => Some(a.state),
            Some(CallWait::Approval { .. }) => panic!("not an approval"),
            None => None,
        };
        assert_eq!(state("asking"), Some(AskState::Asking));
        assert_eq!(state("pending"), Some(AskState::Pending));
        assert_eq!(state("submitted"), Some(AskState::Submitted));
        assert_eq!(state("answered"), Some(AskState::Answered { answers: answered_a() }));
        assert_eq!(state("not"), Some(AskState::NotAnswered { note: Some("skipped".into()) }));
        assert_eq!(state("refused"), None);
        let Some(CallWait::Ask(not)) = call_of(&items, "not").wait else { panic!() };
        assert_eq!(
            Some(&QuestionOption { label: "a".into(), description: String::new() }),
            not.questions[0].options.first()
        );

        let bad = to_items([&response(vec![ResponsePart::ToolCall(call("x", ASK_USER, "{}"))])], &Renders::new(), None)
            .unwrap();
        assert_eq!(call_of(&bad, "x").wait, None);
    }

    #[test]
    fn approvals_and_threads() {
        let m = response(vec![
            ResponsePart::ToolCall(call("p", "t", "{}")),
            ResponsePart::ToolCall(call("s", "t", "{}")),
            ResponsePart::ToolCall(call("n", "t", "{}")),
        ]);
        let mut items = to_items([&m], &Renders::new(), None).unwrap();
        let open = OpenAsks(vec![
            ("p".into(), OpenAsk::Approval("t".into())),
            ("s".into(), OpenAsk::Submitted(Waiting::Approval)),
        ]);
        annotate_approvals(&mut items, &open);
        assert_eq!(
            ["p", "s", "n"].map(|id| call_of(&items, id).wait),
            [
                Some(CallWait::Approval { state: ApprovalState::Pending }),
                Some(CallWait::Approval { state: ApprovalState::Submitted }),
                None
            ]
        );
        annotate_threads(&mut items, &BTreeMap::from([("n".to_owned(), "child".to_owned())]));
        assert_eq!(["p", "s", "n"].map(|id| call_of(&items, id).threads.len()), [0, 0, 1]);
        assert_eq!(call_of(&items, "n").threads, ["child"]);
    }

    #[test]
    fn text_of_items() {
        let r = request(vec![
            RequestPart::Text { text: "look".into() },
            RequestPart::File(file("/api/files/abc/pic.png")),
            RequestPart::Text { text: format_event(&job7(), "done") },
        ]);
        let mut r = r;
        mark_parts(&mut r, vec![event(2, job7(), false)]).unwrap();
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
