//! 运行端口在消息上写下的约定：工具交回给用户的文件与图片。新增约定先在这里登记，解析也只在这里写一份。会话领域自己的
//! 约定（停止、信件、运行上下文、子会话等）在会话领域。
//!
//! | 约定 | 记在哪里 | 谁写 | 谁读 |
//! |---|---|---|---|
//! | [`TOOL_ATTACHMENTS`]：工具挂给用户的文件 | `ToolResult.metadata` | share_file、ask_agent（经 [`attach`]） | 会话条目、实时事件、转交给调用方 |
//! | 工具交回的图片（[`result_images`]） | `ToolResult.content` 里的文件引用 | 运行端口存图（如 MCP 截图、read_file 读图） | 模型（展开）、会话条目、实时事件；不转交调用方 |
//!
//! metadata 的键带 `muliao_` 前缀：以后别的地方往同一个字段写东西时不撞名。

use agenter::{Content, File, FileSource, Message, RequestPart, ToolResult};
use muliao_files::{FileMeta, INLINE_IMAGE_TYPES, parse_file_url};
use muliao_platform::tooling::RunScope;
use serde::{Deserialize, Serialize};

/// 工具结果上挂给用户的文件（`ToolResult.metadata` 的键）：文件一定会显示给用户，不依赖模型在回答里转写引用。
pub const TOOL_ATTACHMENTS: &str = "muliao_attachments";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Attachment {
    /// 文件存储的地址（files::file_url）：界面直接用它显示与下载
    pub url: String,
    pub name: String,
    pub media_type: String,
    /// 能内联显示的图片；其余是下载链接
    pub inline: bool,
}

pub fn attachment_of(meta: &FileMeta) -> Attachment {
    Attachment {
        url: meta.url(),
        name: meta.name.clone(),
        media_type: meta.media_type.clone(),
        inline: meta.is_inline_image(),
    }
}

/// 消息里一个文件引用（存储形态）的展示形式；不是文件存储的引用时为 None（库里只该有这种引用，调用方按数据损坏报）。
pub fn attachment_of_ref(file: &File) -> Option<Attachment> {
    let FileSource::Url(url) = &file.source else { return None };
    let (_, name) = parse_file_url(url)?;
    Some(Attachment {
        url: url.clone(),
        name,
        media_type: file.media_type.clone(),
        inline: INLINE_IMAGE_TYPES.contains(&file.media_type.as_str()),
    })
}

/// 在工具里调用：把这些文件挂到这次调用的结果上给用户看（运行端口在这一步结束时并进结果）。
pub fn attach(scope: &RunScope, call_id: &str, attachments: &[Attachment]) {
    let value = serde_json::to_value(attachments).expect("attachments serialize");
    scope.set_result_metadata(call_id, TOOL_ATTACHMENTS, value);
}

/// 工具结果挂上的文件（历史条目与实时事件共用这一处解析）。不带这个键时为空；带了却解析不了、或地址不是本站文件存储的，
/// 是写入方的 bug，当场 panic（前端会把 url 直接放进 src/href）。
pub fn tool_attachments(result: &ToolResult) -> Vec<Attachment> {
    let Some(value) = result.metadata.as_ref().and_then(|m| m.get(TOOL_ATTACHMENTS)) else { return Vec::new() };
    let attachments: Vec<Attachment> = serde_json::from_value(value.clone())
        .unwrap_or_else(|e| panic!("bad {TOOL_ATTACHMENTS} on a result of {}: {e}", result.tool_name));
    for a in &attachments {
        assert!(parse_file_url(&a.url).is_some(), "attachment url {:?} is not a stored file", a.url);
    }
    attachments
}

/// 工具交回、已存进文件存储的图片的展示形式。它们是给模型看的结果，会话里显示在这次调用的过程里；不是挂给用户的文件，
/// 所以不进 [`shared_attachments`]、不转交调用方。
pub fn result_images(result: &ToolResult) -> Vec<Attachment> {
    result
        .content
        .iter()
        .filter_map(|c| match c {
            Content::File(f) => attachment_of_ref(f),
            Content::Text { .. } => None,
        })
        .collect()
}

/// 这些消息里所有工具结果挂上的文件，按出现顺序。
pub fn shared_attachments(messages: &[Message]) -> Vec<Attachment> {
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
