//! 一条消息：Python 版存的 pydantic-ai 形态 → agenter 的 [`Message`]，同时把 Python 版写在消息上的约定换成 Rust 版的
//! （`muliao_sessions::conventions`、`muliao_runtime::conventions`）。转换的对照经 agenter `examples/migrate_muliao.rs`
//! 在线上库全量验证过（#672），这里照搬，另外做了三件迁移才需要的事：
//!
//! - 请求里的段数会变（一个 user-prompt 带文字和附件时拆成几段），所以 `muliao_events`、`muliao_check_ins`、
//!   `muliao_run_context` 里的段下标按新的位置重排；
//! - 工具结果的结局：`failed` → 出错；`interrupted`（服务重启时没跑完的调用）→ Rust 版的「事后补的结果」标记；其余结果的
//!   `timestamp`（pydantic-ai 在结果产生时记下）→ `muliao_finished_at`（会话页显示结果的时间）；
//! - 生成到一半被打断的回复（pydantic-ai 的 `state = interrupted`）→ Rust 版的 `muliao_interrupted`（压缩判断、会话页用）。
//!
//! 丢掉的（发给模型和界面都用不到）：instructions（每次运行重新生成）、run_id / conversation_id、provider_url、cost 等派生
//! 字段、请求的 `state`。part 上别的 metadata（`muliao_attachments`、`muliao_threads`、`mcp_app`）原样保留：前一个两版
//! 形状相同，后两个 Rust 版不读。

use std::collections::BTreeMap;

use agenter::{
    Content, File, FileSource, Message, Request, RequestPart, Response, ResponsePart, ToolCall, ToolResult, Usage,
    anthropic, openai_responses,
};
use chrono::{DateTime, SecondsFormat, Utc};
use muliao_sessions::conventions::{
    FINISHED_AT, INTERRUPTED, MADE_UP, RUN_CHECK_INS, RUN_CONTEXT, RUN_EVENTS, RUN_STOPPED, STOPPED_TOOL_RESULT,
};
use serde::Deserialize;
use serde_json::value::RawValue;
use serde_json::{Map, Value, json};

/// 转换中值得在报告里说明的情况，各有多少条。
pub type Notes = BTreeMap<String, usize>;

fn note(notes: &mut Notes, what: impl Into<String>) {
    *notes.entry(what.into()).or_default() += 1;
}

fn s(v: &Value) -> String {
    v.as_str().unwrap_or_default().to_string()
}

fn time(v: &Value) -> Option<DateTime<Utc>> {
    v.as_str()?.parse::<DateTime<Utc>>().ok()
}

fn nonempty(m: Map<String, Value>) -> Option<Value> {
    (!m.is_empty()).then_some(Value::Object(m))
}

/// pydantic-ai 的文件内容项（`kind` 是这几种之一）；其余带 `kind` 的对象是工具交回的普通数据（如触发器列表）。
const FILE_KINDS: &[&str] = &["image-url", "document-url", "audio-url", "video-url", "binary"];

fn is_file_item(item: &Value) -> bool {
    item.get("kind").and_then(Value::as_str).is_some_and(|k| FILE_KINDS.contains(&k))
}

fn file_item(item: &Value, notes: &mut Notes) -> Result<File, String> {
    match item["kind"].as_str().unwrap_or_default() {
        "binary" => Err("inline binary content (Python stored files by reference; not expected)".into()),
        _ => {
            let url = s(&item["url"]);
            let media_type = match item["media_type"].as_str() {
                Some(m) => m.to_string(),
                None => {
                    note(notes, "file without media_type (guessed from the URL)");
                    guess_media_type(&url)
                }
            };
            Ok(File { media_type, source: FileSource::Url(url) })
        }
    }
}

fn guess_media_type(url: &str) -> String {
    let ext = url.rsplit('.').next().unwrap_or_default().to_ascii_lowercase();
    match ext.as_str() {
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "pdf" => "application/pdf",
        _ => "application/octet-stream",
    }
    .to_string()
}

/// 保留对象键顺序的 JSON 值（只用来把原文按原顺序缩进排版：`Value` 会把键排成字母序）。
enum Ordered {
    Scalar(Value),
    List(Vec<Ordered>),
    Object(Vec<(String, Ordered)>),
}

impl<'de> Deserialize<'de> for Ordered {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = Ordered;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_bool<E>(self, v: bool) -> Result<Ordered, E> {
                Ok(Ordered::Scalar(Value::Bool(v)))
            }
            fn visit_i64<E>(self, v: i64) -> Result<Ordered, E> {
                Ok(Ordered::Scalar(json!(v)))
            }
            fn visit_u64<E>(self, v: u64) -> Result<Ordered, E> {
                Ok(Ordered::Scalar(json!(v)))
            }
            fn visit_f64<E>(self, v: f64) -> Result<Ordered, E> {
                Ok(Ordered::Scalar(json!(v)))
            }
            fn visit_str<E>(self, v: &str) -> Result<Ordered, E> {
                Ok(Ordered::Scalar(Value::String(v.to_owned())))
            }
            fn visit_unit<E>(self) -> Result<Ordered, E> {
                Ok(Ordered::Scalar(Value::Null))
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut a: A) -> Result<Ordered, A::Error> {
                let mut out = Vec::new();
                while let Some(x) = a.next_element()? {
                    out.push(x);
                }
                Ok(Ordered::List(out))
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut a: A) -> Result<Ordered, A::Error> {
                let mut out = Vec::new();
                while let Some(kv) = a.next_entry()? {
                    out.push(kv);
                }
                Ok(Ordered::Object(out))
            }
        }
        d.deserialize_any(V)
    }
}

impl serde::Serialize for Ordered {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        use serde::ser::{SerializeMap, SerializeSeq};
        match self {
            Ordered::Scalar(v) => v.serialize(s),
            Ordered::List(items) => {
                let mut seq = s.serialize_seq(Some(items.len()))?;
                for i in items {
                    seq.serialize_element(i)?;
                }
                seq.end()
            }
            Ordered::Object(entries) => {
                let mut map = s.serialize_map(Some(entries.len()))?;
                for (k, v) in entries {
                    map.serialize_entry(k, v)?;
                }
                map.end()
            }
        }
    }
}

/// pydantic-ai 交给模型的「请重试」文字（RetryPromptPart.model_response）。raw 是内容的 JSON 原文：错误列表按它的键顺序排版。
fn retry_text(content: &Value, raw: Option<&str>) -> String {
    let body = match content {
        Value::String(t) => t.clone(),
        Value::Array(errors) => format!(
            "{} validation error{}:\n```json\n{}\n```",
            errors.len(),
            if errors.len() == 1 { "" } else { "s" },
            raw.and_then(|r| serde_json::from_str::<Ordered>(r).ok())
                .map(|o| serde_json::to_string_pretty(&o))
                .unwrap_or_else(|| serde_json::to_string_pretty(content))
                .expect("JSON values serialize")
        ),
        other => other.to_string(),
    };
    format!("{body}\n\nFix the errors and try again.")
}

/// raw：这段内容在旧库里的 JSON 原文。结构化的结果按原文变成文字（保留键的顺序，与 Python 版交给模型的一致），不重新序列化。
fn tool_content(content: &Value, raw: Option<&str>, notes: &mut Notes) -> Result<Vec<Content>, String> {
    Ok(match content {
        Value::String(t) => vec![Content::text(t.clone())],
        Value::Array(items) if items.iter().any(is_file_item) => {
            note(notes, "tool result with files");
            let mut out = Vec::new();
            for i in items {
                match i {
                    Value::String(t) => out.push(Content::text(t.clone())),
                    _ if is_file_item(i) => out.push(Content::File(file_item(i, notes)?)),
                    other => out.push(Content::text(other.to_string())),
                }
            }
            out
        }
        other => {
            note(notes, "structured tool result (became its JSON text)");
            let text = match raw {
                Some(r) => r.to_owned(),
                None => serde_json::to_string(other).expect("JSON values serialize"),
            };
            vec![Content::text(text)]
        }
    })
}

/// 消息自己的 metadata：Python 版的约定键与 Rust 版同名；「已停止」Python 版存成 1，Rust 版认布尔值。段下标另由
/// [`remap_indices`] 重排。
fn message_metadata(msg: &Value) -> Result<Map<String, Value>, String> {
    let mut meta = msg["metadata"].as_object().cloned().unwrap_or_default();
    if let Some(v) = meta.get(RUN_STOPPED) {
        let stopped = match v {
            Value::Bool(b) => *b,
            Value::Number(n) => n.as_i64() != Some(0),
            other => return Err(format!("{RUN_STOPPED} is {other}")),
        };
        meta.insert(RUN_STOPPED.into(), Value::Bool(stopped));
    }
    Ok(meta)
}

/// 旧请求的一段在新请求里的位置。
struct OldPart {
    /// 从新请求的第几段开始
    start: usize,
    /// 是 user-prompt 时它的内容项数（每一项成为新请求的一段）；别的段为 None
    items: Option<usize>,
}

fn index(v: &Value) -> Result<usize, String> {
    v.as_u64().map(|i| i as usize).ok_or_else(|| format!("index {v} is not a number"))
}

/// 下标换到新请求的段位置。两边的记法不同：
/// - 运行上下文：Python 版记的是第几个 part，Rust 版也是；
/// - 事件与运行中检查：Python 版记的是「那段用户消息」（运行上下文以外的第一个 user-prompt，一条请求里的信件都并在它里面）
///   内容列表里的第几项，Rust 版记的是第几段。内容列表的每一项正好成为一段，所以是那段的起点加项号。
fn remap_indices(meta: &mut Map<String, Value>, old: &[OldPart]) -> Result<(), String> {
    let context = meta.get(RUN_CONTEXT).map(index).transpose()?;
    if let Some(i) = context {
        let part = old.get(i).ok_or_else(|| format!("run context part {i} is out of range"))?;
        meta.insert(RUN_CONTEXT.into(), json!(part.start));
    }
    for key in [RUN_EVENTS, RUN_CHECK_INS] {
        let Some(v) = meta.get(key) else { continue };
        let list = v.as_array().ok_or_else(|| format!("{key} is not a list"))?;
        let prompt = old
            .iter()
            .enumerate()
            .find(|(i, p)| p.items.is_some() && Some(*i) != context)
            .map(|(_, p)| p)
            .ok_or_else(|| format!("{key} is set but the request has no user prompt"))?;
        let items = prompt.items.unwrap_or_default();
        let mapped = list
            .iter()
            .map(|v| {
                let i = index(v)?;
                if i >= items {
                    return Err(format!("{key} item {i} is out of range"));
                }
                Ok(json!(prompt.start + i))
            })
            .collect::<Result<Vec<_>, _>>()?;
        meta.insert(key.into(), Value::Array(mapped));
    }
    Ok(())
}

fn request(msg: &Value, raw: &[Option<&str>], saved_at: DateTime<Utc>, notes: &mut Notes) -> Result<Request, String> {
    let mut parts = Vec::new();
    let mut old = Vec::new();
    for (n, p) in msg["parts"].as_array().ok_or("no parts")?.iter().enumerate() {
        let start = parts.len();
        let mut items = None;
        match p["part_kind"].as_str().unwrap_or_default() {
            "user-prompt" => match &p["content"] {
                Value::String(t) => {
                    items = Some(1);
                    parts.push(RequestPart::Text { text: t.clone() })
                }
                Value::Array(list) => {
                    items = Some(list.len());
                    for i in list {
                        match i {
                            Value::String(t) => parts.push(RequestPart::Text { text: t.clone() }),
                            _ if is_file_item(i) => {
                                note(notes, "user attachment");
                                parts.push(RequestPart::File(file_item(i, notes)?))
                            }
                            other => return Err(format!("unsupported user content {other}")),
                        }
                    }
                }
                other => return Err(format!("unsupported user content {other}")),
            },
            "tool-return" => parts.push(RequestPart::ToolResult(tool_return(p, raw.get(n).copied().flatten(), notes)?)),
            "retry-prompt" => {
                let text = retry_text(&p["content"], raw.get(n).copied().flatten());
                match p["tool_name"].as_str() {
                    Some(name) => parts.push(RequestPart::ToolResult(ToolResult {
                        call_id: s(&p["tool_call_id"]),
                        tool_name: name.to_string(),
                        content: vec![Content::text(text)],
                        is_error: true,
                        metadata: time(&p["timestamp"])
                            .map(|at| json!({FINISHED_AT: at.to_rfc3339_opts(SecondsFormat::Micros, true)})),
                    })),
                    None => {
                        note(notes, "retry prompt without a tool (became text)");
                        parts.push(RequestPart::Text { text })
                    }
                }
            }
            other => return Err(format!("unsupported request part {other}")),
        }
        old.push(OldPart { start, items });
    }
    if msg["state"].as_str().is_some_and(|st| st != "complete") {
        note(notes, format!("request with state {} (state dropped)", s(&msg["state"])));
    }
    let mut meta = message_metadata(msg)?;
    let context = meta.get(RUN_CONTEXT).and_then(Value::as_u64).map(|i| i as usize);
    remap_indices(&mut meta, &old)?;
    // pydantic-ai 多数请求消息本身不带时间：用用户消息那段（运行上下文以外的 user-prompt）的——会话页上用户消息与事件显示的
    // 就是它，工具结果有自己的结束时间；没有就用第一个带时间的段的，都没有就用保存的时间
    let parts_of = || msg["parts"].as_array().into_iter().flatten();
    let at = time(&msg["timestamp"])
        .or_else(|| {
            parts_of()
                .enumerate()
                .filter(|(i, p)| p["part_kind"] == "user-prompt" && Some(*i) != context)
                .find_map(|(_, p)| time(&p["timestamp"]))
        })
        .or_else(|| parts_of().find_map(|p| time(&p["timestamp"])))
        .unwrap_or_else(|| {
            note(notes, "request without a timestamp (used the time it was saved)");
            saved_at
        });
    Ok(Request { parts, timestamp: at, metadata: nonempty(meta) })
}

fn tool_return(p: &Value, raw: Option<&str>, notes: &mut Notes) -> Result<ToolResult, String> {
    let outcome = p["outcome"].as_str().unwrap_or("success");
    let mut meta = p["metadata"].as_object().cloned().unwrap_or_default();
    match outcome {
        // 停止时事后补的结果：Python 版只凭文字认（与 Rust 版同一句），Rust 版记 muliao_made_up = stopped
        "success" if p["content"].as_str() == Some(STOPPED_TOOL_RESULT) => {
            note(notes, "tool result made up when stopped (marked as made up)");
            meta.insert(MADE_UP.into(), json!("stopped"));
        }
        "success" | "failed" => {
            if let Some(at) = time(&p["timestamp"]) {
                meta.insert(FINISHED_AT.into(), json!(at.to_rfc3339_opts(SecondsFormat::Micros, true)));
            }
        }
        // 服务重启时没跑完、事后补上的结果：Rust 版同样的情况记作 muliao_made_up = interrupted
        "interrupted" => {
            note(notes, "tool result outcome interrupted (marked as made up)");
            meta.insert(MADE_UP.into(), json!("interrupted"));
        }
        other => {
            note(notes, format!("tool result outcome {other} (kept in metadata)"));
            meta.insert("outcome".into(), json!(other));
        }
    }
    Ok(ToolResult {
        call_id: s(&p["tool_call_id"]),
        tool_name: s(&p["tool_name"]),
        content: tool_content(&p["content"], raw, notes)?,
        is_error: outcome == "failed",
        metadata: nonempty(meta),
    })
}

fn response(msg: &Value, notes: &mut Notes) -> Result<Response, String> {
    let provider = msg["provider_name"].as_str();
    let protocol = match provider {
        Some("anthropic") => anthropic::PROTOCOL,
        Some("openai") => openai_responses::PROTOCOL,
        None => {
            note(notes, "response without provider");
            "unknown"
        }
        Some(other) => return Err(format!("unsupported provider {other}")),
    };
    let mut parts = Vec::new();
    for p in msg["parts"].as_array().ok_or("no parts")? {
        // part 自己的 provider_name 为空时属于整条回复的 provider
        let from = p["provider_name"].as_str().or(provider);
        let openai = from == Some("openai");
        match p["part_kind"].as_str().unwrap_or_default() {
            "text" => {
                let mut meta = Map::new();
                if openai {
                    if let Some(id) = p["id"].as_str() {
                        meta.insert("id".into(), json!(id));
                    }
                    if let Some(phase) = p["provider_details"].get("phase") {
                        meta.insert("phase".into(), phase.clone());
                    }
                }
                parts.push(ResponsePart::Text { text: s(&p["content"]), meta: nonempty(meta) })
            }
            "thinking" => parts.push(thinking(p, from, notes)),
            "tool-call" => {
                let args = match &p["args"] {
                    // 没有参数的调用 pydantic-ai 存成空串（它按 {} 解释）；新版按原文显示，统一成 {}
                    Value::String(a) if a.trim().is_empty() => {
                        note(notes, "tool call without arguments (empty string became {})");
                        "{}".into()
                    }
                    Value::String(a) => a.clone(),
                    Value::Null => "{}".into(),
                    other => other.to_string(),
                };
                let meta = if openai { p["id"].as_str().map(|id| json!({"id": id})) } else { None };
                parts.push(ResponsePart::ToolCall(ToolCall {
                    id: s(&p["tool_call_id"]),
                    name: s(&p["tool_name"]),
                    args,
                    meta,
                }))
            }
            other => return Err(format!("unsupported response part {other}")),
        }
    }
    let mut meta = message_metadata(msg)?;
    if msg["state"].as_str() == Some("interrupted") {
        note(notes, "response cut off while generating (marked interrupted)");
        meta.insert(INTERRUPTED.into(), json!(true));
    } else if msg["state"].as_str().is_some_and(|st| st != "complete") {
        return Err(format!("response with state {}", msg["state"]));
    }
    let u = &msg["usage"];
    let n = |k: &str| u[k].as_u64().unwrap_or(0);
    let finish_reason =
        msg["provider_details"]["finish_reason"].as_str().or(msg["finish_reason"].as_str()).map(String::from);
    Ok(Response {
        parts,
        protocol: protocol.into(),
        model: s(&msg["model_name"]),
        response_id: msg["provider_response_id"].as_str().map(String::from),
        finish_reason,
        usage: Usage {
            input_tokens: n("input_tokens"),
            output_tokens: n("output_tokens"),
            cache_read_tokens: n("cache_read_tokens"),
            cache_write_tokens: n("cache_write_tokens"),
        },
        timestamp: time(&msg["timestamp"]).ok_or("response without a timestamp")?,
        metadata: nonempty(meta),
    })
}

fn thinking(p: &Value, from: Option<&str>, notes: &mut Notes) -> ResponsePart {
    let content = s(&p["content"]);
    let raw: Vec<String> = p["provider_details"]["raw_content"]
        .as_array()
        .map(|a| a.iter().map(s).filter(|t| !t.is_empty()).collect())
        .unwrap_or_default();
    let sig = p["signature"].as_str().filter(|s| !s.is_empty());
    let (text, meta) = match from {
        Some("anthropic") if p["id"] == "redacted_thinking" => {
            note(notes, "redacted thinking");
            (String::new(), sig.map(|d| json!({"redacted": d})))
        }
        Some("anthropic") => {
            if sig.is_none() {
                note(notes, "anthropic thinking without signature (will not be replayed)");
            }
            (content, sig.map(|d| json!({"signature": d})))
        }
        Some("openai") => {
            let mut meta = Map::new();
            if let Some(id) = p["id"].as_str() {
                meta.insert("id".into(), json!(id));
            }
            if let Some(sig) = sig {
                meta.insert("encrypted_content".into(), json!(sig));
            }
            let text = if content.is_empty() && !raw.is_empty() {
                meta.insert("raw".into(), json!(true));
                note(notes, "raw reasoning text");
                raw.join("\n\n")
            } else {
                if !raw.is_empty() {
                    note(notes, "raw reasoning dropped (summary kept)");
                }
                content
            };
            (text, nonempty(meta))
        }
        other => {
            note(notes, format!("thinking from provider {other:?} (no replay data)"));
            (content, None)
        }
    };
    ResponsePart::Thinking { text, meta }
}

/// 旧消息里各段内容的 JSON 原文（`Value` 不保留对象键的顺序）。
#[derive(Deserialize)]
struct RawMessage<'a> {
    #[serde(borrow, default)]
    parts: Vec<RawPart<'a>>,
}

#[derive(Deserialize)]
struct RawPart<'a> {
    #[serde(borrow, default)]
    content: Option<&'a RawValue>,
}

/// 转换一条消息（saved_at：它在旧库里的保存时间，请求没带时间时用）。转出来的写出去再读回来必须一模一样。
pub fn convert(payload: &str, saved_at: DateTime<Utc>, notes: &mut Notes) -> Result<Message, String> {
    let msg: Value = serde_json::from_str(payload).map_err(|e| format!("not JSON: {e}"))?;
    let m = match msg["kind"].as_str() {
        Some("request") => {
            let raw: RawMessage = serde_json::from_str(payload).map_err(|e| format!("not JSON: {e}"))?;
            let contents: Vec<Option<&str>> = raw.parts.iter().map(|p| p.content.map(RawValue::get)).collect();
            Message::User(request(&msg, &contents, saved_at, notes)?)
        }
        Some("response") => Message::Assistant(response(&msg, notes)?),
        other => return Err(format!("unknown message kind {other:?}")),
    };
    let back: Message = serde_json::from_str(&serde_json::to_string(&m).expect("messages serialize"))
        .map_err(|e| format!("round trip: {e}"))?;
    if back != m {
        return Err("round trip changed the message".into());
    }
    Ok(m)
}

#[cfg(test)]
mod tests {
    use muliao_sessions::conventions::{
        MadeUp, check_in_indices, event_indices, finished_at, is_interrupted, made_up, run_context_index,
    };

    use super::*;

    fn at() -> DateTime<Utc> {
        "2026-11-12T10:00:00Z".parse().unwrap()
    }

    #[test]
    fn indices_follow_parts_that_split() {
        let payload = json!({
            "kind": "request",
            "timestamp": "2026-11-12T10:00:00Z",
            // Python 版：运行上下文是第 1 个 part；事件是「那段用户消息」（第 2 个 part）内容里的第 2、3 项
            "metadata": {"muliao_run_context": 1, "muliao_events": [2, 3], "muliao_check_ins": [3], "muliao_stopped": 1},
            "parts": [
                {"part_kind": "tool-return", "tool_call_id": "a", "tool_name": "t", "content": "ok"},
                {"part_kind": "user-prompt", "content": "now"},
                {"part_kind": "user-prompt", "content": ["看图", {"kind": "image-url", "url": "/api/files/a/x.png", "media_type": "image/png"},
                    "<event source=\"job:1\">\nok\n</event>", "<event source=\"job:2\">\nrunning\n</event>"]}
            ]
        })
        .to_string();
        let Message::User(r) = convert(&payload, at(), &mut Notes::new()).unwrap() else { panic!() };
        // 新：结果、上下文、看图、图、事件、事件
        assert_eq!(r.parts.len(), 6);
        assert_eq!(run_context_index(&r), Some(1));
        assert_eq!(event_indices(&r), [4, 5]);
        assert_eq!(check_in_indices(&r), [5]);
        for i in [4, 5] {
            let RequestPart::Text { text } = &r.parts[i] else { panic!() };
            assert!(muliao_sessions::conventions::parse_event(text).is_ok());
        }
        assert!(muliao_sessions::conventions::is_stopped(&Message::User(r)));
    }

    #[test]
    fn tool_results_carry_time_outcome_and_data() {
        let payload = json!({
            "kind": "request",
            "parts": [
                {"part_kind": "tool-return", "tool_call_id": "a", "tool_name": "t", "content": "ok",
                 "timestamp": "2026-11-12T10:00:01.5Z", "outcome": "success",
                 "metadata": {"muliao_attachments": []}},
                {"part_kind": "tool-return", "tool_call_id": "b", "tool_name": "t", "content": "x",
                 "timestamp": "2026-11-12T10:00:02Z", "outcome": "interrupted"},
                {"part_kind": "tool-return", "tool_call_id": "c", "tool_name": "trigger_list",
                 "content": [{"kind": "schedule", "name": "n"}], "timestamp": "2026-11-12T10:00:02Z"},
                {"part_kind": "tool-return", "tool_call_id": "d", "tool_name": "t", "content": "boom",
                 "timestamp": "2026-11-12T10:00:02Z", "outcome": "failed"}
            ]
        })
        .to_string();
        let mut notes = Notes::new();
        let Message::User(r) = convert(&payload, at(), &mut notes).unwrap() else { panic!() };
        // 请求本身没带时间：用第一个段的
        assert_eq!(r.timestamp, "2026-11-12T10:00:01.5Z".parse::<DateTime<Utc>>().unwrap());
        let results: Vec<&ToolResult> = r
            .parts
            .iter()
            .map(|p| match p {
                RequestPart::ToolResult(t) => t,
                _ => panic!(),
            })
            .collect();
        assert_eq!(finished_at(results[0]), Some("2026-11-12T10:00:01.5Z".parse().unwrap()));
        assert!(results[0].metadata.as_ref().unwrap().get("muliao_attachments").is_some());
        assert_eq!(made_up(results[1]), Some(MadeUp::Interrupted));
        assert_eq!(finished_at(results[1]), None);
        // 带 kind 的普通数据不是文件：整体成为 JSON 文字
        assert!(matches!(&results[2].content[..], [Content::Text { text }] if text.starts_with("[{")));
        assert!(results[3].is_error);
    }

    #[test]
    fn structured_results_keep_the_original_text() {
        let payload = r#"{"kind":"request","timestamp":"2026-11-12T10:00:00Z","parts":[{"part_kind":"tool-return","tool_call_id":"a","tool_name":"t","content":[{"name":"n","kind":"schedule"}]}]}"#;
        let Message::User(r) = convert(payload, at(), &mut Notes::new()).unwrap() else { panic!() };
        let RequestPart::ToolResult(t) = &r.parts[0] else { panic!() };
        assert!(matches!(&t.content[..], [Content::Text { text }] if text == r#"[{"name":"n","kind":"schedule"}]"#));
    }

    #[test]
    fn interrupted_response_is_marked() {
        let payload = json!({
            "kind": "response", "state": "interrupted", "provider_name": "anthropic", "model_name": "m",
            "timestamp": "2026-11-12T10:00:00Z", "usage": {"input_tokens": 3},
            "parts": [{"part_kind": "text", "content": "半句"}]
        })
        .to_string();
        let Message::Assistant(r) = convert(&payload, at(), &mut Notes::new()).unwrap() else { panic!() };
        assert!(is_interrupted(&r));
        assert_eq!(r.usage.input_tokens, 3);
    }

    #[test]
    fn bad_indices_are_errors() {
        let payload = json!({
            "kind": "request", "timestamp": "2026-11-12T10:00:00Z", "metadata": {"muliao_events": [5]},
            "parts": [{"part_kind": "user-prompt", "content": ["x"]}]
        })
        .to_string();
        assert!(convert(&payload, at(), &mut Notes::new()).unwrap_err().contains("out of range"));
    }
}
