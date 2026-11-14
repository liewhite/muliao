//! 会话领域在 agenter 消息上加的约定，集中登记在这里：新增约定先在这里登记，解析也只在这里写一份。工具交回给用户的
//! 文件与图片是运行端口的约定（`muliao_runtime::conventions`），它在工具结果上写的那部分（[`ResultFiles`]）平铺进
//! [`ResultMeta`]。
//!
//! 消息是存储与回放的唯一形式（库里一行一条 [`Message`]），所以「这一轮是被停止的」这类事实都记在消息自己的 metadata
//! 上（不发给模型、随消息持久化），不另存副本。metadata 的形状按位置各是一个 serde 结构（键都带 `muliao_` 前缀）：
//! 请求上 [`RequestMeta`]、回复上 [`ResponseMeta`]、工具结果上 [`ResultMeta`]。读库时（`history::decode`）整条消息的
//! metadata 按它们检查一次（[`check_metadata`]），之后的读取不再处理坏数据：读不出就是代码的 bug，当场 panic。请求、回复上不认识的键也算坏数据：加了新键的
//! 版本上线后再回滚，旧版读不了那些消息（数据还在，重新上线新版即恢复）。
//!
//! | 约定 | 记在哪里 | 谁写 | 谁读 |
//! |---|---|---|---|
//! | `muliao_stopped`：运行被用户停止 | 停止时最后那条消息 | 停止（halt） | 会话条目（「已停止」）、续跑（不重发标了它的请求） |
//! | `muliao_interrupted`：生成到一半被停的回复 | 回复 | 停止 | 会话条目；压缩判断（[`measured_responses`]） |
//! | `muliao_made_up`：事后补的结果（停止、中断时没出结果的调用） | 工具结果（[`MadeUp`]） | 停止、续跑 | 模型看结果文字；会话条目（没有结束时间） |
//! | `muliao_parts`：这条请求里哪些段是信件内容、各是什么（[`PartMark`]：事件的来源与是否运行中检查，消息的发送方） | 请求 | 交出信件时 | 会话条目、热历史（哪句是用户说的） |
//! | `muliao_run_context`：这次运行的运行上下文（当前时间等）是第几段 | 请求 | 每步保存 | 模型；会话条目不展示 |
//! | `muliao_finished_at`：工具调用结束的时刻 | 工具结果 | 每步保存 | 会话条目（结果的时间） |
//! | 「调用是否已有结果」 | 工具结果；没结果的只可能在最后一条回复里 | — | [`OpenCalls`] |

use std::collections::HashSet;

use agenter::{Message, Request, RequestPart, Response, ResponsePart, ToolCall, ToolResult};
use chrono::{DateTime, Utc};
use muliao_runtime::conventions::{ResultFiles, SharedFile};
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::intake::origins::Origin;
use crate::mailbox::EventSource;
use muliao_platform::errors::{Result, broken};

/// 停止时还没出结果的工具调用的结果：副作用可能已经发生，告诉模型它没有完成。
pub(crate) const STOPPED_TOOL_RESULT: &str =
    "Stopped by the user before this tool call finished; it may have partly run.";
/// 一步在执行工具时被打断（进程退出，或这一步失败）：续跑不重新执行这些调用——副作用可能已经发生，而我们无从知道。
pub(super) const INTERRUPTED_TOOL_RESULT: &str = "Interrupted: this run was interrupted while the call was running (the \
     service restarted, or the step failed), and the call was NOT re-run automatically. It may or may not have taken \
     effect; check its effects before deciding whether to run it again.";

/// 压缩切点落在一条回复之前时补在留下部分前面的一条用户消息（由切点推导，不存库）。
pub(super) const CONTINUATION: &str =
    "The earlier part of this conversation is summarized in your instructions. Continue from there.";

/// 事后补的结果是哪一种。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum MadeUp {
    Stopped,
    Interrupted,
}

impl MadeUp {
    fn text(self) -> &'static str {
        match self {
            MadeUp::Stopped => STOPPED_TOOL_RESULT,
            MadeUp::Interrupted => INTERRUPTED_TOOL_RESULT,
        }
    }
}

fn is_false(b: &bool) -> bool {
    !b
}

/// 请求（用户一方的消息）上的 metadata。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestMeta {
    #[serde(rename = "muliao_stopped", default, skip_serializing_if = "is_false")]
    stopped: bool,
    #[serde(rename = "muliao_parts", default, skip_serializing_if = "Vec::is_empty")]
    parts: Vec<PartMark>,
    #[serde(rename = "muliao_run_context", default, skip_serializing_if = "Option::is_none")]
    run_context: Option<usize>,
}

/// 回复上的 metadata。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResponseMeta {
    #[serde(rename = "muliao_stopped", default, skip_serializing_if = "is_false")]
    stopped: bool,
    #[serde(rename = "muliao_interrupted", default, skip_serializing_if = "is_false")]
    interrupted: bool,
}

/// 工具结果上的 metadata：会话领域的，加上运行端口写下的（[`ResultFiles`]）。平铺了别人的结构，不能拒绝不认识的键：
/// 旧版写下、现在不读的键（`mcp_app`、`muliao_threads`）读时忽略；库里已有的工具结果不再改写，不会丢掉它们。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
struct ResultMeta {
    #[serde(rename = "muliao_made_up", default, skip_serializing_if = "Option::is_none")]
    made_up: Option<MadeUp>,
    #[serde(rename = "muliao_finished_at", default, skip_serializing_if = "Option::is_none")]
    finished_at: Option<DateTime<Utc>>,
    #[serde(flatten)]
    files: ResultFiles,
}

fn try_read<T: DeserializeOwned + Default>(metadata: &Option<Value>) -> std::result::Result<T, String> {
    match metadata {
        None => Ok(T::default()),
        Some(v) => T::deserialize(v).map_err(|e| e.to_string()),
    }
}

/// 读 metadata：消息读库时已检查过（[`check_metadata`]），其余的都由这里写出，所以读不出是 bug。
fn read<T: DeserializeOwned + Default>(metadata: &Option<Value>) -> T {
    try_read(metadata).unwrap_or_else(|e| panic!("metadata {metadata:?} was checked or written here: {e}"))
}

/// 写回 metadata；什么都没有时为 None。
fn write<T: Serialize>(metadata: &mut Option<Value>, meta: &T) {
    let value = serde_json::to_value(meta).expect("metadata serializes");
    *metadata = (value != Value::Object(Map::new())).then_some(value);
}

fn edit<T: Serialize + DeserializeOwned + Default>(metadata: &mut Option<Value>, change: impl FnOnce(&mut T)) {
    let mut meta: T = read(metadata);
    change(&mut meta);
    write(metadata, &meta);
}

/// 读库边界：这条消息上各处 metadata 的形状对不对（不对是数据与代码不一致，说明是哪一处）。
pub(super) fn check_metadata(message: &Message) -> std::result::Result<(), String> {
    match message {
        Message::User(r) => {
            try_read::<RequestMeta>(&r.metadata).map_err(|e| format!("request metadata: {e}"))?;
            for p in &r.parts {
                if let RequestPart::ToolResult(t) = p {
                    try_read::<ResultMeta>(&t.metadata)
                        .map_err(|e| format!("metadata of a result of {}: {e}", t.tool_name))?;
                }
            }
        }
        Message::Assistant(r) => {
            try_read::<ResponseMeta>(&r.metadata).map_err(|e| format!("response metadata: {e}"))?;
        }
    }
    Ok(())
}

/// 这条消息标着「已停止」。
pub(crate) fn is_stopped(message: &Message) -> bool {
    match message {
        Message::User(r) => read::<RequestMeta>(&r.metadata).stopped,
        Message::Assistant(r) => read::<ResponseMeta>(&r.metadata).stopped,
    }
}

/// 标上「已停止」：界面据此显示，续跑据此不重发这条请求。
pub(crate) fn mark_stopped(message: &mut Message) {
    match message {
        Message::User(r) => edit(&mut r.metadata, |m: &mut RequestMeta| m.stopped = true),
        Message::Assistant(r) => edit(&mut r.metadata, |m: &mut ResponseMeta| m.stopped = true),
    }
}

/// 生成到一半被停下的回复（停止时按已收到的部分保存）。
pub(crate) fn interrupted_response(parts: Vec<ResponsePart>, protocol: &str, model: &str) -> Response {
    let mut metadata = None;
    write(&mut metadata, &ResponseMeta { interrupted: true, ..Default::default() });
    Response {
        parts,
        protocol: protocol.to_owned(),
        model: model.to_owned(),
        response_id: None,
        finish_reason: None,
        usage: Default::default(),
        timestamp: chrono::Utc::now(),
        metadata,
    }
}

pub(crate) fn is_interrupted(response: &Response) -> bool {
    read::<ResponseMeta>(&response.metadata).interrupted
}

/// 给没出结果的调用补上的结果。
pub(crate) fn made_up_result(call: &ToolCall, kind: MadeUp) -> ToolResult {
    let mut metadata = None;
    write(&mut metadata, &ResultMeta { made_up: Some(kind), ..Default::default() });
    ToolResult {
        call_id: call.id.clone(),
        tool_name: call.name.clone(),
        content: vec![agenter::Content::text(kind.text())],
        is_error: false,
        metadata,
    }
}

/// 这个结果是事后补的（不是工具交回的）：它产生的时刻不是调用结束的时刻。
pub(super) fn made_up(result: &ToolResult) -> Option<MadeUp> {
    read::<ResultMeta>(&result.metadata).made_up
}

/// 工具调用结束的时刻（保存结果时记下）；没记的（事后补的结果、记下这条约定之前的结果）为 None。
pub(crate) fn finished_at(result: &ToolResult) -> Option<DateTime<Utc>> {
    read::<ResultMeta>(&result.metadata).finished_at
}

/// 记下工具调用结束的时刻。
pub(crate) fn set_finished_at(result: &mut ToolResult, at: DateTime<Utc>) {
    edit(&mut result.metadata, |m: &mut ResultMeta| m.finished_at = Some(at));
}

/// 工具结果挂给用户的文件（运行端口写下的，[`ResultFiles`]）。
pub(crate) fn tool_attachments(result: &ToolResult) -> Vec<SharedFile> {
    read::<ResultMeta>(&result.metadata).files.attachments
}

/// 这些消息里所有工具结果挂上的文件，按出现顺序。
pub(crate) fn shared_attachments(messages: &[Message]) -> Vec<SharedFile> {
    messages
        .iter()
        .filter_map(|m| match m {
            Message::User(r) => Some(r),
            Message::Assistant(_) => None,
        })
        .flat_map(|r| &r.parts)
        .filter_map(|p| match p {
            RequestPart::ToolResult(t) => Some(tool_attachments(t)),
            _ => None,
        })
        .flatten()
        .collect()
}

/// 这些结果合成一条请求；没有则 None。
pub(crate) fn results_request(results: Vec<ToolResult>) -> Option<Request> {
    (!results.is_empty()).then(|| Request {
        parts: results.into_iter().map(RequestPart::ToolResult).collect(),
        timestamp: chrono::Utc::now(),
        metadata: None,
    })
}

/// 请求里一段信件内容是什么：第几段（`parts` 的下标）、哪一种。这些事实只存在这里，读时不从文字反推。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct PartMark {
    pub part: usize,
    #[serde(flatten)]
    pub kind: PartKind,
}

/// 存成 `{"event": "job:7", "check_in": false}` 或 `{"sender": "user"}`（与 part 平铺在一起）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub(crate) enum PartKind {
    /// 会话事件：交给模型的文字（[`format_event`]）是由来源派生的写法
    Event {
        #[serde(rename = "event", with = "raw_source")]
        source: EventSource,
        check_in: bool,
    },
    /// 一封消息信件的文字或附件，以及它的发送方来源
    Message {
        #[serde(rename = "sender")]
        origin: Origin,
    },
}

/// 事件来源在 metadata 里按交给模型的写法存（[`EventSource::raw`]）。
mod raw_source {
    use serde::{Deserialize, Deserializer, Serializer};

    use crate::mailbox::EventSource;

    pub(super) fn serialize<S: Serializer>(source: &EventSource, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&source.raw())
    }

    pub(super) fn deserialize<'de, D: Deserializer<'de>>(d: D) -> Result<EventSource, D::Error> {
        let raw = String::deserialize(d)?;
        EventSource::parse(&raw).ok_or_else(|| serde::de::Error::custom(format!("unknown event source {raw:?}")))
    }
}

impl PartMark {
    /// 同一段在前面多了 n 段的请求里。
    pub(crate) fn shifted(self, n: usize) -> Self {
        PartMark { part: self.part + n, ..self }
    }
}

const EVENT_END: &str = "\n</event>";

fn event_head(source: &EventSource) -> String {
    format!("<event source=\"{}\">\n", source.raw())
}

/// 交给模型的一条事件。
pub(crate) fn format_event(source: &EventSource, content: &str) -> String {
    format!("{}{content}{EVENT_END}", event_head(source))
}

/// 事件段里的事件内容（去掉 [`format_event`] 套的外壳）；只用于展示，外壳对不上时整段原样展示。
pub(super) fn event_content<'a>(text: &'a str, source: &EventSource) -> &'a str {
    text.strip_prefix(event_head(source).as_str()).and_then(|t| t.strip_suffix(EVENT_END)).unwrap_or(text)
}

/// 这条请求里记下的各段，按记下的顺序。
pub(crate) fn part_marks(request: &Request) -> Vec<PartMark> {
    read::<RequestMeta>(&request.metadata).parts
}

/// 整条请求记下的各段换成这些（新建请求时）。
pub(crate) fn set_part_marks(request: &mut Request, marks: &[PartMark]) {
    edit(&mut request.metadata, |m: &mut RequestMeta| m.parts = marks.to_vec());
}

/// 接在请求末尾的这些段记下来（交出信件时；part 已是在整条请求里的下标）。
pub(crate) fn mark_parts(request: &mut Request, marks: Vec<PartMark>) {
    edit(&mut request.metadata, |m: &mut RequestMeta| m.parts.extend(marks));
}

/// 把运行上下文作为独立的一段文字放进这条请求，并记下它的下标。放在工具结果之后、别的内容之前：模型先看到「现在
/// 几点」再看这次要处理的内容。已记下的各段下标随之后移。
pub(crate) fn attach_run_context(request: &mut Request, text: &str) {
    let at = request.parts.iter().position(|p| !matches!(p, RequestPart::ToolResult(_))).unwrap_or(request.parts.len());
    request.parts.insert(at, RequestPart::Text { text: text.to_owned() });
    edit(&mut request.metadata, |m: &mut RequestMeta| {
        assert!(m.run_context.is_none(), "request already carries a run context");
        for mark in &mut m.parts {
            if mark.part >= at {
                mark.part += 1;
            }
        }
        m.run_context = Some(at);
    });
}

/// 这条请求里哪一段是运行上下文。
pub(super) fn run_context_index(request: &Request) -> Option<usize> {
    read::<RequestMeta>(&request.metadata).run_context
}

/// 报告了上下文大小的回复，按时间顺序：完整的回复（报 0 也算一次测量，由压缩判断告警），生成到一半被停的只在收到了
/// 输入用量时算。压缩判断只认这些。
pub(super) fn measured_responses(history: &[Message]) -> Vec<&Response> {
    history
        .iter()
        .filter_map(|m| match m {
            Message::Assistant(r) if !is_interrupted(r) || r.usage.input_tokens > 0 => Some(r),
            _ => None,
        })
        .collect()
}

/// 一条回复给用户的文字（会话最近的回答、运行的回答都按这一条取）。
pub(crate) fn answer_text(response: &Response) -> String {
    response.text()
}

/// 这条消息给出结果的调用 id。
pub(super) fn results_in(message: &Message) -> impl Iterator<Item = &str> {
    let parts: &[RequestPart] = match message {
        Message::User(r) => &r.parts,
        Message::Assistant(_) => &[],
    };
    parts.iter().filter_map(|p| match p {
        RequestPart::ToolResult(t) => Some(t.call_id.as_str()),
        _ => None,
    })
}

/// 这条消息发出的工具调用。
pub(super) fn calls_in(message: &Message) -> Vec<&ToolCall> {
    match message {
        Message::Assistant(r) => r.tool_calls().collect(),
        Message::User(_) => Vec::new(),
    }
}

/// 逐条读消息，到目前为止还没有结果的工具调用（按出现顺序，带所在消息的序号）。「调用是否已有结果」只在这里定义：
/// 同 id 的工具结果出现了就有了。[`unanswered_calls`] 与压缩的分步都用它。
#[derive(Default)]
pub(super) struct OpenCalls<'a> {
    open: Vec<(usize, &'a ToolCall)>,
    read: usize,
}

impl<'a> OpenCalls<'a> {
    pub(crate) fn read(&mut self, message: &'a Message) {
        let at = self.read;
        self.read += 1;
        self.open.extend(calls_in(message).into_iter().map(|c| (at, c)));
        let done: HashSet<&str> = results_in(message).collect();
        if !done.is_empty() {
            self.open.retain(|(_, c)| !done.contains(c.id.as_str()));
        }
    }

    pub(crate) fn is_empty(&self) -> bool {
        self.open.is_empty()
    }
}

/// 还没有结果的工具调用，按出现顺序（定义见 [`OpenCalls`]）。
///
/// 库里的历史守着一条不变式（提供方的要求）：每条回复的调用在下一条回复之前都有结果，所以没结果的调用只可能在最后一条
/// 回复里。破了说明有一处写错了，当场报错，不往末尾补结果（补在末尾的结果离开了它的调用，提供方会拒绝之后的每一次请求）。
pub(crate) fn unanswered_calls(messages: &[Message]) -> Result<Vec<ToolCall>> {
    let mut open = OpenCalls::default();
    for m in messages {
        open.read(m);
    }
    let last = messages.iter().rposition(|m| matches!(m, Message::Assistant(_)));
    if let Some((_, c)) = open.open.iter().find(|(at, _)| Some(*at) != last) {
        return Err(broken(format!("history has a tool call without a result before its last response: {}", c.id)));
    }
    Ok(open.open.into_iter().map(|(_, c)| c.clone()).collect())
}

/// 消息里可按关键字搜到的文字：用户一方的文字（不含运行上下文）、模型的文字回复、工具调用（工具名与参数的各个字符串值）。
/// 不含工具结果（文件内容、命令输出，搜什么都会命中很多）和思考。各段用换行连接；没有文字时为空串。
pub(super) fn searchable_text(message: &Message) -> String {
    let mut chunks: Vec<String> = Vec::new();
    match message {
        Message::User(r) => {
            let context = run_context_index(r);
            for (i, p) in r.parts.iter().enumerate() {
                if let RequestPart::Text { text } = p
                    && Some(i) != context
                {
                    chunks.push(text.clone());
                }
            }
        }
        Message::Assistant(r) => {
            for p in &r.parts {
                match p {
                    ResponsePart::Text { text, .. } => chunks.push(text.clone()),
                    ResponsePart::ToolCall(c) => {
                        chunks.push(c.name.clone());
                        match serde_json::from_str::<Value>(&c.args) {
                            Ok(v) => strings(&v, &mut chunks),
                            Err(_) => chunks.push(c.args.clone()),
                        }
                    }
                    ResponsePart::Thinking { .. } => {}
                }
            }
        }
    }
    chunks.retain(|c| !c.is_empty());
    chunks.join("\n")
}

fn strings(v: &Value, out: &mut Vec<String>) {
    match v {
        Value::String(s) => out.push(s.clone()),
        Value::Array(a) => a.iter().for_each(|x| strings(x, out)),
        Value::Object(o) => o.values().for_each(|x| strings(x, out)),
        _ => {}
    }
}

#[cfg(test)]
mod tests {
    use muliao_platform::ids::JobId;

    use super::*;

    fn call(id: &str) -> ResponsePart {
        ResponsePart::ToolCall(ToolCall {
            id: id.into(),
            name: "t".into(),
            args: "{\"cmd\":\"ls -la\"}".into(),
            meta: None,
        })
    }

    fn response(parts: Vec<ResponsePart>) -> Message {
        Message::Assistant(Response {
            parts,
            protocol: "anthropic".into(),
            model: "m".into(),
            response_id: None,
            finish_reason: None,
            usage: Default::default(),
            timestamp: chrono::Utc::now(),
            metadata: None,
        })
    }

    fn results(ids: &[&str]) -> Message {
        let calls: Vec<ToolCall> =
            ids.iter().map(|i| ToolCall { id: (*i).into(), name: "t".into(), args: "{}".into(), meta: None }).collect();
        Message::User(results_request(calls.iter().map(|c| made_up_result(c, MadeUp::Interrupted)).collect()).unwrap())
    }

    #[test]
    fn unanswered_calls_only_in_the_last_response() {
        let h = vec![response(vec![call("a"), call("b")]), results(&["a"])];
        assert_eq!(unanswered_calls(&h).unwrap().iter().map(|c| c.id.as_str()).collect::<Vec<_>>(), ["b"]);
        let broken = vec![response(vec![call("a")]), response(vec![])];
        assert!(unanswered_calls(&broken).is_err());
    }

    #[test]
    fn events_round_trip_and_run_context_shifts_indices() {
        let job = EventSource::Job { job_id: JobId::new(1) };
        let e = format_event(&job, "done\nok");
        assert_eq!(event_content(&e, &job), "done\nok");
        assert_eq!(event_content("<event>x</event>", &job), "<event>x</event>");

        let mut r = Request {
            parts: vec![RequestPart::Text { text: "hi".into() }, RequestPart::Text { text: e.clone() }],
            timestamp: chrono::Utc::now(),
            metadata: None,
        };
        let event = PartKind::Event { source: job, check_in: true };
        let user = PartKind::Message { origin: Origin::User };
        mark_parts(&mut r, vec![PartMark { part: 0, kind: user.clone() }, PartMark { part: 1, kind: event.clone() }]);
        attach_run_context(&mut r, "now");
        assert_eq!(run_context_index(&r), Some(0));
        assert_eq!(part_marks(&r), [PartMark { part: 1, kind: user }, PartMark { part: 2, kind: event }]);
        assert_eq!(
            r.metadata,
            Some(serde_json::json!({
                "muliao_parts": [{ "part": 1, "sender": "user" }, { "part": 2, "event": "job:1", "check_in": true }],
                "muliao_run_context": 0,
            }))
        );
        assert_eq!(searchable_text(&Message::User(r)), format!("hi\n{e}"));
    }

    #[test]
    fn searchable_text_of_calls_has_values_not_keys() {
        let m = response(vec![ResponsePart::Text { text: "ok".into(), meta: None }, call("a")]);
        assert_eq!(searchable_text(&m), "ok\nt\nls -la");
    }

    #[test]
    fn stop_marks_and_made_up_results() {
        let mut m = results(&["a"]);
        assert!(!is_stopped(&m));
        mark_stopped(&mut m);
        assert!(is_stopped(&m));
        let Message::User(r) = &m else { unreachable!() };
        let RequestPart::ToolResult(t) = &r.parts[0] else { unreachable!() };
        assert_eq!(made_up(t), Some(MadeUp::Interrupted));
    }

    #[test]
    fn finished_at_round_trips() {
        let mut r =
            ToolResult { call_id: "a".into(), tool_name: "t".into(), content: vec![], is_error: false, metadata: None };
        assert_eq!(finished_at(&r), None);
        let at = DateTime::parse_from_rfc3339("2026-11-12T10:00:00.123456Z").unwrap().with_timezone(&Utc);
        set_finished_at(&mut r, at);
        assert_eq!(finished_at(&r), Some(at));
    }

    #[test]
    fn metadata_is_checked_when_read() {
        let mut m = response(vec![]);
        mark_stopped(&mut m);
        let Message::Assistant(r) = &mut m else { unreachable!() };
        assert_eq!(r.metadata, Some(serde_json::json!({ "muliao_stopped": true })));
        r.metadata = Some(serde_json::json!({ "muliao_stoped": true }));
        assert!(check_metadata(&m).is_err());

        let bad = |metadata: Value| {
            check_metadata(&Message::User(Request {
                parts: vec![],
                timestamp: chrono::Utc::now(),
                metadata: Some(metadata),
            }))
        };
        assert!(
            bad(serde_json::json!({ "muliao_parts": [{ "part": 0, "event": "nope", "check_in": false }] })).is_err()
        );
        assert!(bad(serde_json::json!({ "muliao_parts": "[]" })).is_err());
        assert!(bad(serde_json::json!({ "muliao_run_context": "0" })).is_err());
        let mut t =
            ToolResult { call_id: "a".into(), tool_name: "t".into(), content: vec![], is_error: false, metadata: None };
        t.metadata = Some(serde_json::json!({ "mcp_app": {}, "muliao_made_up": "stopped" }));
        assert_eq!(made_up(&t), Some(MadeUp::Stopped));
        t.metadata = Some(serde_json::json!({ "muliao_made_up": "later" }));
        let r = Request { parts: vec![RequestPart::ToolResult(t)], timestamp: chrono::Utc::now(), metadata: None };
        assert!(check_metadata(&Message::User(r)).unwrap_err().contains("result of t"));
    }
}
