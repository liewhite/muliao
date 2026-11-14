//! 运行端口在消息上写下的约定：工具交回给用户的文件与图片。新增约定先在这里登记，解析也只在这里写一份。会话领域自己的
//! 约定（停止、信件、运行上下文、子会话等）在会话领域。
//!
//! | 约定 | 记在哪里 | 谁写 | 谁读 |
//! |---|---|---|---|
//! | `muliao_attachments`：工具挂给用户的文件（[`SharedFile`]，只存文件编号、名字与类型） | `ToolResult.metadata`（[`ResultFiles`]） | share_file、ask_agent（经 [`attach`]） | 会话领域（它的结果 metadata 平铺了 [`ResultFiles`]）：会话条目、实时事件、转交给调用方 |
//! | 工具交回的图片（[`result_images`]） | `ToolResult.content` 里的文件引用 | 运行端口存图（如 MCP 截图、read_file 读图） | 模型（展开）、会话条目、实时事件；不转交调用方 |
//!
//! 工具结果的 metadata 在 agenter 交回时是空的，运行端口这一步结束时写下 [`ResultFiles`]（[`crate::runner`]），会话领域
//! 之后再往同一个对象里加自己的键（都带 `muliao_` 前缀）。

use agenter::{Content, File, FileSource, ToolResult};
use muliao_files::{FileMeta, file_url, parse_file_url};
use muliao_platform::ids::MediaType;
use muliao_platform::tooling::RunScope;
use serde::{Deserialize, Serialize};

/// 运行端口写在工具结果 metadata 上的部分。
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResultFiles {
    /// 挂给用户的文件：一定会显示给用户，不依赖模型在回答里转写引用
    #[serde(rename = "muliao_attachments", default, skip_serializing_if = "Vec::is_empty")]
    pub attachments: Vec<SharedFile>,
}

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
    let meta = ResultFiles { attachments: files.to_vec() };
    scope.set_result_metadata(call_id, serde_json::to_value(meta).expect("shared files serialize"));
}

/// 工具交回、已存进文件存储的图片的展示形式。它们是给模型看的结果，会话里显示在这次调用的过程里；不是挂给用户的文件，
/// 所以不进挂给用户的文件、不转交调用方。
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
