//! 信件交给模型的样子：消息是原文加附件引用，事件套上来源（[`format_event`]），迟到的回答写成文字。一次请求里的信件都
//! 接在这条请求末尾，各段是什么（事件的来源、是否检查，消息的发送方）在构造时就记下（`PARTS`），界面据此单独显示，不从文字反推。

use std::path::Path;

use agenter::{Request, RequestPart};
use muliao_files::FileStore;
use muliao_runtime::attachments::{copy_to_workspace, references};

use crate::answers::CallResult;
use crate::conventions::{PartKind, PartMark, format_event, mark_parts, set_part_marks};
use crate::error::{Result, broken};
use crate::mailbox::{Mail, MailBody};

/// 一批信件的内容段，以及各段是什么（part 从 0 起，在这批内容里；迟到的回答不记）。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Content {
    pub parts: Vec<RequestPart>,
    pub marks: Vec<PartMark>,
}

impl Content {
    pub fn is_empty(&self) -> bool {
        self.parts.is_empty()
    }
}

/// 回答信件在这里只会是迟到的：按时的回答作为挂起调用的结果交回。消息的附件复制到 agent 的工作目录（派生副本，原件在
/// 文件存储里）。
pub async fn content_of(files: &FileStore, workspace: &Path, mail: &[Mail]) -> Result<Content> {
    let mut c = Content::default();
    for m in mail {
        match &m.body {
            MailBody::Answer { answers, .. } => {
                let results: Vec<CallResult> = answers.values().cloned().collect();
                c.parts.push(RequestPart::Text { text: CallResult::late_text(&results) });
            }
            MailBody::Event { source, content, check_in } => {
                let kind = PartKind::Event { source: source.clone(), check_in: *check_in };
                c.marks.push(PartMark { part: c.parts.len(), kind });
                c.parts.push(RequestPart::Text { text: format_event(source, content) });
            }
            MailBody::Message { origin, content, attachments, .. } => {
                let metas = files.referenced(attachments.clone()).await?;
                copy_to_workspace(files, &metas, workspace)
                    .await
                    .map_err(|e| broken(format!("copying attachments of mail {} into the workspace: {e}", m.id)))?;
                let from = c.parts.len();
                if !content.trim().is_empty() {
                    c.parts.push(RequestPart::Text { text: content.clone() });
                }
                c.parts.extend(references(&metas));
                let kind = PartKind::Message { origin: *origin };
                c.marks.extend((from..c.parts.len()).map(|part| PartMark { part, kind: kind.clone() }));
            }
        }
    }
    Ok(c)
}

/// 把内容接在这条请求末尾，并记下各段是什么。
pub fn append_content(request: &mut Request, content: Content) -> Result<()> {
    let offset = request.parts.len();
    request.parts.extend(content.parts);
    mark_parts(request, content.marks.into_iter().map(|m| m.shifted(offset)).collect())
}

/// 新的一条请求，内容就是这些。
pub fn request_of(content: Content) -> Request {
    let mut r = Request { parts: content.parts, timestamp: chrono::Utc::now(), metadata: None };
    set_part_marks(&mut r, &content.marks);
    r
}
