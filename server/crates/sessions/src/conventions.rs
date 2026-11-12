//! 会话领域在 agenter 消息上加的约定，集中登记在这里：新增约定先在这里登记，解析也只在这里写一份。工具交回给用户的
//! 文件与图片是运行端口的约定（`muliao_runtime::conventions`）。
//!
//! 消息是存储与回放的唯一形式（库里一行一条 [`Message`]），所以「这一轮是被停止的」这类事实都记在消息自己的 metadata
//! 上（不发给模型、随消息持久化），不另存副本。键都带 `muliao_` 前缀。
//!
//! | 约定 | 记在哪里 | 谁写 | 谁读 |
//! |---|---|---|---|
//! | [`RUN_STOPPED`]：运行被用户停止 | 停止时最后那条消息的 metadata | 停止（halt） | 会话条目（「已停止」）、续跑（不重发标了它的请求） |
//! | [`INTERRUPTED`]：生成到一半被停的回复 | 回复的 metadata | 停止 | 会话条目；压缩判断（[`measured_responses`]） |
//! | [`MADE_UP`]：事后补的结果（停止、中断时没出结果的调用） | 工具结果的 metadata（`stopped` / `interrupted`） | 停止、续跑 | 模型看结果文字；会话条目（没有结束时间） |
//! | [`RUN_EVENTS`] / [`RUN_CHECK_INS`]：这条请求里哪些段是事件、其中哪些是运行中检查 | 请求的 metadata（`parts` 的下标） | 交出信件时 | 会话条目 |
//! | [`RUN_CONTEXT`]：这次运行的运行上下文（当前时间等） | 单独一段文字，请求的 metadata 记下标 | 每步保存 | 模型；会话条目不展示 |
//! | [`FINISHED_AT`]：工具调用结束的时刻 | 工具结果的 metadata（RFC3339） | 每步保存 | 会话条目（结果的时间） |
//! | 「调用是否已有结果」 | 工具结果；没结果的只可能在最后一条回复里 | — | [`unanswered_calls`] |

use std::collections::HashSet;

use agenter::{Message, Request, RequestPart, Response, ResponsePart, ToolCall, ToolResult};
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{Map, Value};

use crate::error::{Result, broken};

pub const RUN_STOPPED: &str = "muliao_stopped";
pub const INTERRUPTED: &str = "muliao_interrupted";
pub const MADE_UP: &str = "muliao_made_up";
pub const RUN_EVENTS: &str = "muliao_events";
pub const RUN_CHECK_INS: &str = "muliao_check_ins";
pub const RUN_CONTEXT: &str = "muliao_run_context";
pub const FINISHED_AT: &str = "muliao_finished_at";

/// 停止时还没出结果的工具调用的结果：副作用可能已经发生，告诉模型它没有完成。
pub const STOPPED_TOOL_RESULT: &str = "Stopped by the user before this tool call finished; it may have partly run.";
/// 一步在执行工具时被打断（进程退出，或这一步失败）：续跑不重新执行这些调用——副作用可能已经发生，而我们无从知道。
pub const INTERRUPTED_TOOL_RESULT: &str = "Interrupted: this run was interrupted while the call was running (the \
     service restarted, or the step failed), and the call was NOT re-run automatically. It may or may not have taken \
     effect; check its effects before deciding whether to run it again.";

/// 压缩切点落在一条回复之前时补在留下部分前面的一条用户消息（由切点推导，不存库）。
pub const CONTINUATION: &str =
    "The earlier part of this conversation is summarized in your instructions. Continue from there.";

/// 事后补的结果是哪一种。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MadeUp {
    Stopped,
    Interrupted,
}

impl MadeUp {
    fn as_str(self) -> &'static str {
        match self {
            MadeUp::Stopped => "stopped",
            MadeUp::Interrupted => "interrupted",
        }
    }

    fn text(self) -> &'static str {
        match self {
            MadeUp::Stopped => STOPPED_TOOL_RESULT,
            MadeUp::Interrupted => INTERRUPTED_TOOL_RESULT,
        }
    }
}

fn meta_of(metadata: &Option<Value>) -> Option<&Map<String, Value>> {
    metadata.as_ref().and_then(Value::as_object)
}

fn meta_mut(metadata: &mut Option<Value>) -> &mut Map<String, Value> {
    if !matches!(metadata, Some(Value::Object(_))) {
        *metadata = Some(Value::Object(Map::new()));
    }
    match metadata {
        Some(Value::Object(m)) => m,
        _ => unreachable!(),
    }
}

fn metadata_of(message: &Message) -> &Option<Value> {
    match message {
        Message::User(r) => &r.metadata,
        Message::Assistant(r) => &r.metadata,
    }
}

fn metadata_mut(message: &mut Message) -> &mut Option<Value> {
    match message {
        Message::User(r) => &mut r.metadata,
        Message::Assistant(r) => &mut r.metadata,
    }
}

/// 这条消息标着「已停止」。
pub fn is_stopped(message: &Message) -> bool {
    meta_of(metadata_of(message)).and_then(|m| m.get(RUN_STOPPED)).and_then(Value::as_bool).unwrap_or(false)
}

/// 标上「已停止」：界面据此显示，续跑据此不重发这条请求。
pub fn mark_stopped(message: &mut Message) {
    meta_mut(metadata_mut(message)).insert(RUN_STOPPED.into(), Value::Bool(true));
}

/// 生成到一半被停下的回复（停止时按已收到的部分保存）。
pub fn interrupted_response(parts: Vec<ResponsePart>, protocol: &str, model: &str) -> Response {
    let mut metadata = None;
    meta_mut(&mut metadata).insert(INTERRUPTED.into(), Value::Bool(true));
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

pub fn is_interrupted(response: &Response) -> bool {
    meta_of(&response.metadata).and_then(|m| m.get(INTERRUPTED)).and_then(Value::as_bool).unwrap_or(false)
}

/// 给没出结果的调用补上的结果。
pub fn made_up_result(call: &ToolCall, kind: MadeUp) -> ToolResult {
    let mut metadata = None;
    meta_mut(&mut metadata).insert(MADE_UP.into(), Value::from(kind.as_str()));
    ToolResult {
        call_id: call.id.clone(),
        tool_name: call.name.clone(),
        content: vec![agenter::Content::text(kind.text())],
        is_error: false,
        metadata,
    }
}

/// 这个结果是事后补的（不是工具交回的）：它产生的时刻不是调用结束的时刻。
pub fn made_up(result: &ToolResult) -> Option<MadeUp> {
    match meta_of(&result.metadata).and_then(|m| m.get(MADE_UP)).and_then(Value::as_str) {
        Some("stopped") => Some(MadeUp::Stopped),
        Some("interrupted") => Some(MadeUp::Interrupted),
        _ => None,
    }
}

/// 工具调用结束的时刻（保存结果时记下）；没记的（事后补的结果、记下这条约定之前的结果）为 None。值不是 RFC3339 时间是
/// 写入方的 bug，当场 panic。
pub fn finished_at(result: &ToolResult) -> Option<DateTime<Utc>> {
    let value = meta_of(&result.metadata)?.get(FINISHED_AT)?;
    let parsed = value.as_str().and_then(|s| DateTime::parse_from_rfc3339(s).ok());
    let at = parsed.unwrap_or_else(|| panic!("bad {FINISHED_AT} {value} on a result of {}", result.tool_name));
    Some(at.with_timezone(&Utc))
}

/// 记下工具调用结束的时刻。
pub fn set_finished_at(result: &mut ToolResult, at: DateTime<Utc>) {
    meta_mut(&mut result.metadata)
        .insert(FINISHED_AT.into(), Value::from(at.to_rfc3339_opts(SecondsFormat::Micros, true)));
}

/// 这些结果合成一条请求；没有则 None。
pub fn results_request(results: Vec<ToolResult>) -> Option<Request> {
    (!results.is_empty()).then(|| Request {
        parts: results.into_iter().map(RequestPart::ToolResult).collect(),
        timestamp: chrono::Utc::now(),
        metadata: None,
    })
}

/// 交给模型的一条事件。source 是发送方（如 `job:12`），不含引号（投信时校验）。
pub fn format_event(source: &str, content: &str) -> String {
    format!("<event source=\"{source}\">\n{content}\n</event>")
}

/// [`format_event`] 的逆运算；对不上是数据与代码不一致。
pub fn parse_event(text: &str) -> Result<(&str, &str)> {
    let bad = || {
        broken(format!(
            "stored event does not follow the event format: {:?}",
            text.chars().take(200).collect::<String>()
        ))
    };
    let rest = text.strip_prefix("<event source=\"").ok_or_else(bad)?;
    let (source, rest) = rest.split_once("\">\n").ok_or_else(bad)?;
    let content = rest.strip_suffix("\n</event>").ok_or_else(bad)?;
    if source.contains('"') {
        return Err(bad());
    }
    Ok((source, content))
}

fn indices(metadata: &Option<Value>, key: &str) -> Vec<usize> {
    meta_of(metadata)
        .and_then(|m| m.get(key))
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(Value::as_u64).map(|i| i as usize).collect())
        .unwrap_or_default()
}

/// 这条请求里哪些段（`parts` 的下标）是会话事件。
pub fn event_indices(request: &Request) -> Vec<usize> {
    indices(&request.metadata, RUN_EVENTS)
}

/// 其中哪些是运行中检查。
pub fn check_in_indices(request: &Request) -> Vec<usize> {
    indices(&request.metadata, RUN_CHECK_INS)
}

/// 记下这条请求里哪些段是事件、哪些是检查（交出信件时）。
pub fn mark_events(request: &mut Request, events: &[usize], check_ins: &[usize]) {
    if events.is_empty() {
        return;
    }
    let m = meta_mut(&mut request.metadata);
    let mut all = indices(&Some(Value::Object(m.clone())), RUN_EVENTS);
    all.extend_from_slice(events);
    let mut checks = indices(&Some(Value::Object(m.clone())), RUN_CHECK_INS);
    checks.extend_from_slice(check_ins);
    m.insert(RUN_EVENTS.into(), Value::from(all));
    if !checks.is_empty() {
        m.insert(RUN_CHECK_INS.into(), Value::from(checks));
    }
}

/// 把运行上下文作为独立的一段文字放进这条请求，并记下它的下标。放在工具结果之后、别的内容之前：模型先看到「现在
/// 几点」再看这次要处理的内容。已有的事件下标随之后移。
pub fn attach_run_context(request: &mut Request, text: &str) {
    assert!(run_context_index(request).is_none(), "request already carries a run context");
    let at = request.parts.iter().position(|p| !matches!(p, RequestPart::ToolResult(_))).unwrap_or(request.parts.len());
    request.parts.insert(at, RequestPart::Text { text: text.to_owned() });
    let shift = |v: Vec<usize>| -> Vec<usize> { v.into_iter().map(|i| if i >= at { i + 1 } else { i }).collect() };
    let (events, checks) = (shift(event_indices(request)), shift(check_in_indices(request)));
    let m = meta_mut(&mut request.metadata);
    if !events.is_empty() {
        m.insert(RUN_EVENTS.into(), Value::from(events));
    }
    if !checks.is_empty() {
        m.insert(RUN_CHECK_INS.into(), Value::from(checks));
    }
    m.insert(RUN_CONTEXT.into(), Value::from(at));
}

/// 这条请求里哪一段是运行上下文。
pub fn run_context_index(request: &Request) -> Option<usize> {
    meta_of(&request.metadata).and_then(|m| m.get(RUN_CONTEXT)).and_then(Value::as_u64).map(|i| i as usize)
}

/// 报告了上下文大小的回复，按时间顺序：完整的回复（报 0 也算一次测量，由压缩判断告警），生成到一半被停的只在收到了
/// 输入用量时算。压缩判断只认这些。
pub fn measured_responses(history: &[Message]) -> Vec<&Response> {
    history
        .iter()
        .filter_map(|m| match m {
            Message::Assistant(r) if !is_interrupted(r) || r.usage.input_tokens > 0 => Some(r),
            _ => None,
        })
        .collect()
}

/// 一条回复给用户的文字（会话最近的回答、运行的回答都按这一条取）。
pub fn answer_text(response: &Response) -> String {
    response.text()
}

/// 这条消息给出结果的调用 id。
pub fn results_in(message: &Message) -> impl Iterator<Item = &str> {
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
pub fn calls_in(message: &Message) -> Vec<&ToolCall> {
    match message {
        Message::Assistant(r) => r.tool_calls().collect(),
        Message::User(_) => Vec::new(),
    }
}

/// 还没有结果的工具调用，按出现顺序。「调用是否已有结果」只在这里定义。
///
/// 库里的历史守着一条不变式（提供方的要求）：每条回复的调用在下一条回复之前都有结果，所以没结果的调用只可能在最后一条
/// 回复里。破了说明有一处写错了，当场报错，不往末尾补结果（补在末尾的结果离开了它的调用，提供方会拒绝之后的每一次请求）。
pub fn unanswered_calls(messages: &[Message]) -> Result<Vec<ToolCall>> {
    let done: HashSet<&str> = messages.iter().flat_map(results_in).collect();
    let last = messages.iter().rposition(|m| matches!(m, Message::Assistant(_)));
    let mut unanswered = Vec::new();
    for (i, m) in messages.iter().enumerate() {
        for c in calls_in(m) {
            if done.contains(c.id.as_str()) {
                continue;
            }
            if Some(i) != last {
                return Err(broken(format!(
                    "history has a tool call without a result before its last response: {}",
                    c.id
                )));
            }
            unanswered.push(c.clone());
        }
    }
    Ok(unanswered)
}

/// 消息里可按关键字搜到的文字：用户一方的文字（不含运行上下文）、模型的文字回复、工具调用（工具名与参数的各个字符串值）。
/// 不含工具结果（文件内容、命令输出，搜什么都会命中很多）和思考。各段用换行连接；没有文字时为空串。
pub fn searchable_text(message: &Message) -> String {
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
        let e = format_event("job:1", "done\nok");
        assert_eq!(parse_event(&e).unwrap(), ("job:1", "done\nok"));
        assert!(parse_event("<event>x</event>").is_err());

        let mut r = Request {
            parts: vec![RequestPart::Text { text: "hi".into() }, RequestPart::Text { text: e.clone() }],
            timestamp: chrono::Utc::now(),
            metadata: None,
        };
        mark_events(&mut r, &[1], &[1]);
        attach_run_context(&mut r, "now");
        assert_eq!(run_context_index(&r), Some(0));
        assert_eq!(event_indices(&r), [2]);
        assert_eq!(check_in_indices(&r), [2]);
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
}
