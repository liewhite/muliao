//! 消息里的附件有两种形态，只在发给模型的那一刻转换：
//!
//! - 存储形态（数据库、界面、重放）：用户消息里的 `RequestPart::File` 与工具结果里的 `Content::File`，来源是
//!   `files::file_url` 的引用。界面直接用它显示图片和下载链接。
//! - 模型形态：每次模型请求前由 [`for_model`] 展开。用户的附件换成一段说明（名称、类型、大小、在工作目录里的路径），
//!   能内联的图片再附上内容；工具结果里的图片换回内容（不加说明：它没有复制进工作目录）。图片内容先按 provider 的上限
//!   整理（[`crate::images::fit`]：超限的缩小或切段，原件不动）。展开只改这一次请求，不改历史，库里不会出现附件和图片内容的副本（工具交回的别的文件，如 MCP 的 PDF，原样留在
//!   结果里，同 Python 版）。
//!
//! 工具交回的图片（如 MCP 浏览器的截图、read_file 读的图片文件）走同一套：[`store_tool_images`] 在工具执行完、结果写进
//! 历史之前把能内联的图片存进文件存储，结果里换成引用。
//!
//! 附件同时复制进 agent 工作目录的 `uploads/<id>/<名称>`（[`copy_to_workspace`]），供文件工具与 shell 处理。那是派生副本，
//! agent 可以随意修改；不可变的原件只在文件存储里。

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use agenter::{Content, File, FileSource, Message, Request, RequestPart, ToolResult};
use muliao_files::{FileMeta, FileStore, INLINE_IMAGE_TYPES, parse_file_url};
use muliao_platform::errors::Error;

use crate::images;

pub const UPLOADS_DIR: &str = "uploads";

/// 附件的存储形态（会话领域把用户上传的文件放进消息时用）。
pub fn references(files: &[FileMeta]) -> Vec<RequestPart> {
    files
        .iter()
        .map(|f| RequestPart::File(File { media_type: f.media_type.clone(), source: FileSource::Url(f.url()) }))
        .collect()
}

/// 附件在 agent 工作目录里的相对路径：按 id 分目录，同名的不同文件互不覆盖，名称保持原样。
pub fn workspace_path(meta: &FileMeta) -> PathBuf {
    Path::new(UPLOADS_DIR).join(&meta.id).join(&meta.name)
}

/// 每次执行这一轮都重新复制：重试时覆盖成原件，结果总是一样。
pub async fn copy_to_workspace(store: &FileStore, files: &[FileMeta], workspace: &Path) -> std::io::Result<()> {
    for meta in files {
        let target = workspace.join(workspace_path(meta));
        tokio::fs::create_dir_all(target.parent().expect("upload path has a parent")).await?;
        tokio::fs::copy(store.blob_path(meta), &target).await?;
    }
    Ok(())
}

pub fn describe(meta: &FileMeta) -> String {
    format!(
        "[Attached file {:?} ({}, {} bytes), copied into your workspace at {}]",
        meta.name,
        meta.media_type,
        meta.size,
        workspace_path(meta).display()
    )
}

/// 消息里一个文件引用的 id；不是文件存储的引用（存储形态以外的文件）为 None，原样发出。
fn ref_id(file: &File) -> Option<String> {
    match &file.source {
        FileSource::Url(url) => parse_file_url(url).map(|(id, _)| id),
        FileSource::Data(_) => None,
    }
}

/// 一条消息里要展开的文件引用：用户的附件、工具结果里存下的图片。
fn refs_in(request: &Request) -> impl Iterator<Item = String> + '_ {
    request.parts.iter().flat_map(|p| -> Vec<String> {
        match p {
            RequestPart::File(f) => ref_id(f).into_iter().collect(),
            RequestPart::ToolResult(t) => t
                .content
                .iter()
                .filter_map(|c| match c {
                    Content::File(f) => ref_id(f),
                    Content::Text { .. } => None,
                })
                .collect(),
            RequestPart::Text { .. } => Vec::new(),
        }
    })
}

/// 一张图片的模型形态：按 provider 的上限整理，整理过就在前面写明做了什么；解不成图片的换成一句说明，不发给 provider（它
/// 会拒绝整次请求，而图片在历史里，之后每次请求都会失败）。
async fn image_content(store: &FileStore, meta: &FileMeta) -> Result<Vec<Content>, Error> {
    let data = store.read(meta).await?;
    let fitted = tokio::task::spawn_blocking(move || images::fit(&data))
        .await
        .unwrap_or_else(|e| std::panic::resume_unwind(e.into_panic()));
    Ok(match fitted {
        Err(e) => {
            tracing::warn!(file_id = %meta.id, media_type = %meta.media_type, error = %e, "image not decodable, not sent to the model");
            vec![Content::text(format!("[image {:?} could not be decoded as an image, so it is not shown]", meta.name))]
        }
        Ok(f) => {
            f.note
                .map(Content::text)
                .into_iter()
                .chain(f.tiles.into_iter().map(|t| {
                    Content::File(File { media_type: t.media_type.to_owned(), source: FileSource::Data(t.data) })
                }))
                .collect()
        }
    })
}

/// 这次请求要发出的历史：文件引用展开成模型形态（见模块说明）。历史本身不动。
pub async fn for_model(store: &FileStore, history: &[Message]) -> Result<Vec<Message>, Error> {
    let mut ids: Vec<String> = Vec::new();
    for m in history {
        if let Message::User(r) = m {
            for id in refs_in(r) {
                if !ids.contains(&id) {
                    ids.push(id);
                }
            }
        }
    }
    if ids.is_empty() {
        return Ok(history.to_vec());
    }
    let metas: HashMap<String, FileMeta> =
        store.referenced(ids).await?.into_iter().map(|m| (m.id.clone(), m)).collect();
    let mut out = Vec::with_capacity(history.len());
    for m in history {
        let Message::User(r) = m else {
            out.push(m.clone());
            continue;
        };
        let mut parts = Vec::with_capacity(r.parts.len());
        for p in &r.parts {
            match p {
                RequestPart::File(f) if ref_id(f).is_some() => {
                    let meta = &metas[&ref_id(f).expect("checked")];
                    parts.push(RequestPart::Text { text: describe(meta) });
                    if meta.is_inline_image() {
                        parts.extend(image_content(store, meta).await?.into_iter().map(|c| match c {
                            Content::Text { text } => RequestPart::Text { text },
                            Content::File(f) => RequestPart::File(f),
                        }));
                    }
                }
                RequestPart::ToolResult(t)
                    if t.content.iter().any(|c| matches!(c, Content::File(f) if ref_id(f).is_some())) =>
                {
                    let mut content = Vec::with_capacity(t.content.len());
                    for c in &t.content {
                        match c {
                            Content::File(f) if ref_id(f).is_some() => {
                                content.extend(image_content(store, &metas[&ref_id(f).expect("checked")]).await?);
                            }
                            other => content.push(other.clone()),
                        }
                    }
                    parts.push(RequestPart::ToolResult(ToolResult { content, ..t.clone() }));
                }
                other => parts.push(other.clone()),
            }
        }
        out.push(Message::User(Request { parts, ..r.clone() }));
    }
    Ok(out)
}

/// 文件存储里图片的扩展名（同 Python mimetypes.guess_extension）
fn extension(media_type: &str) -> &'static str {
    match media_type {
        "image/png" => ".png",
        "image/jpeg" => ".jpg",
        "image/gif" => ".gif",
        "image/webp" => ".webp",
        _ => "",
    }
}

/// 工具结果里能内联的图片存进文件存储、换成引用（见模块说明）。其余内容（文字、PDF 等别的文件）原样留着。存不下（如超过
/// 上传上限）是这次运行的失败。
pub async fn store_tool_images(store: &FileStore, result: &mut ToolResult) -> Result<(), Error> {
    for c in &mut result.content {
        let Content::File(File { media_type, source: FileSource::Data(data) }) = c else { continue };
        if !INLINE_IMAGE_TYPES.contains(&media_type.as_str()) {
            continue;
        }
        let name = format!("{}{}", result.tool_name, extension(media_type));
        let meta = store.put(&name, media_type, data.as_slice()).await?;
        *c = Content::File(File { media_type: meta.media_type.clone(), source: FileSource::Url(meta.url()) });
    }
    Ok(())
}
