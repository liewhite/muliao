//! 运行端口在消息上写下的约定：工具交回给用户的文件与图片。新增约定先在这里登记，解析也只在这里写一份。会话领域自己的
//! 约定（停止、信件、运行上下文、子会话等）在会话领域。
//!
//! | 约定 | 记在哪里 | 谁写 | 谁读 |
//! |---|---|---|---|
//! | [`TOOL_ATTACHMENTS`]：工具挂给用户的文件（[`SharedFile`]，只存文件编号、名字与类型） | `ToolResult.metadata` | share_file、ask_agent（经 [`attach`]） | 会话条目、实时事件、转交给调用方 |
//! | 工具交回的图片（[`result_images`]） | `ToolResult.content` 里的文件引用 | 运行端口存图（如 MCP 截图、read_file 读图） | 模型（展开）、会话条目、实时事件；不转交调用方 |
//!
//! metadata 的键带 `muliao_` 前缀：以后别的地方往同一个字段写东西时不撞名。

use agenter::{Content, File, FileSource, Message, RequestPart, ToolResult};
use muliao_files::{FileMeta, file_url, parse_file_url};
use muliao_platform::ids::MediaType;
use muliao_platform::tooling::RunScope;
use serde::{Deserialize, Serialize};

/// 工具结果上挂给用户的文件（`ToolResult.metadata` 的键）：文件一定会显示给用户，不依赖模型在回答里转写引用。
pub const TOOL_ATTACHMENTS: &str = "muliao_attachments";

/// 挂给用户的一个文件（存储形态）：文件存储里的哪个文件，以及展示要用的名字与类型。地址、能否内联由它派生（[`Attachment`]）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
pub struct SharedFile {
    pub file_id: String,
    pub name: String,
    pub media_type: MediaType,
}

impl SharedFile {
    pub fn of(meta: &FileMeta) -> Self {
        SharedFile { file_id: meta.id.clone(), name: meta.name.clone(), media_type: meta.media_type.clone() }
    }
}

/// 一个文件的展示形式（界面用）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct Attachment {
    /// 文件存储的地址（files::file_url）：界面直接用它显示与下载
    pub url: String,
    pub name: String,
    pub media_type: MediaType,
    /// 能内联显示的图片；其余是下载链接
    pub inline: bool,
}

impl From<&SharedFile> for Attachment {
    fn from(f: &SharedFile) -> Self {
        Attachment {
            url: file_url(&f.file_id, &f.name),
            name: f.name.clone(),
            media_type: f.media_type.clone(),
            inline: f.media_type.is_inline_image(),
        }
    }
}

/// 消息里一个文件引用（存储形态）的展示形式；不是文件存储的引用时为 None（库里只该有这种引用，调用方按数据损坏报）。
pub fn attachment_of_ref(file: &File) -> Option<Attachment> {
    let FileSource::Url(url) = &file.source else { return None };
    let (_, name) = parse_file_url(url)?;
    let media_type = MediaType::parse(&file.media_type).ok()?;
    Some(Attachment { url: url.clone(), name, inline: media_type.is_inline_image(), media_type })
}

/// 在工具里调用：把这些文件挂到这次调用的结果上给用户看（运行端口在这一步结束时并进结果）。
pub fn attach(scope: &RunScope, call_id: &str, files: &[SharedFile]) {
    let value = serde_json::to_value(files).expect("shared files serialize");
    scope.set_result_metadata(call_id, TOOL_ATTACHMENTS, value);
}

/// 工具结果挂上的文件（历史条目与实时事件共用这一处解析）。不带这个键时为空；读库边界：形状不对是数据与代码不一致，
/// 报出是哪个工具的结果。
pub fn tool_attachments(result: &ToolResult) -> Result<Vec<SharedFile>, String> {
    let Some(value) = result.metadata.as_ref().and_then(|m| m.get(TOOL_ATTACHMENTS)) else { return Ok(Vec::new()) };
    serde_json::from_value(value.clone())
        .map_err(|e| format!("bad {TOOL_ATTACHMENTS} on a result of {}: {e}", result.tool_name))
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
pub fn shared_attachments(messages: &[Message]) -> Result<Vec<SharedFile>, String> {
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
        .collect::<Result<Vec<_>, _>>()
        .map(|v| v.into_iter().flatten().collect())
}
