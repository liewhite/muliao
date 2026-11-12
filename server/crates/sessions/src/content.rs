//! 信件交给模型的样子：消息是原文加附件引用，事件套上来源（[`format_event`]），迟到的回答写成文字。一次请求里的信件都
//! 接在这条请求末尾，哪些段是事件在构造时就记下（`RUN_EVENTS`），界面据此单独显示，不从文字反推。

use std::path::Path;

use agenter::{Request, RequestPart};
use muliao_files::FileStore;
use muliao_runtime::attachments::{copy_to_workspace, references};

use crate::answers::CallResult;
use crate::conventions::{format_event, mark_events};
use crate::error::{Result, broken};
use crate::mailbox::{Mail, MailKind};

/// 一批信件的内容段，以及其中哪些段是事件、哪些事件是运行中检查（下标从 0 起，在这批内容里）。
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Content {
    pub parts: Vec<RequestPart>,
    pub events: Vec<usize>,
    pub check_ins: Vec<usize>,
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
        match m.kind {
            MailKind::Answer => {
                let results: Vec<CallResult> = m.answers.values().cloned().collect();
                c.parts.push(RequestPart::Text { text: CallResult::late_text(&results) });
            }
            MailKind::Event => {
                let source = m.source.as_deref().ok_or_else(|| broken(format!("event mail {} has no source", m.id)))?;
                c.events.push(c.parts.len());
                if m.check_in {
                    c.check_ins.push(c.parts.len());
                }
                c.parts.push(RequestPart::Text { text: format_event(source, &m.content) });
            }
            MailKind::Message => {
                let metas = files.referenced(m.attachments.clone()).await?;
                copy_to_workspace(files, &metas, workspace)
                    .await
                    .map_err(|e| broken(format!("copying attachments of mail {} into the workspace: {e}", m.id)))?;
                if !m.content.trim().is_empty() {
                    c.parts.push(RequestPart::Text { text: m.content.clone() });
                }
                c.parts.extend(references(&metas));
            }
        }
    }
    Ok(c)
}

/// 把内容接在这条请求末尾，并记下其中的事件段。
pub fn append_content(request: &mut Request, content: Content) {
    let offset = request.parts.len();
    let events: Vec<usize> = content.events.iter().map(|i| i + offset).collect();
    let checks: Vec<usize> = content.check_ins.iter().map(|i| i + offset).collect();
    request.parts.extend(content.parts);
    mark_events(request, &events, &checks);
}

/// 新的一条请求，内容就是这些。
pub fn request_of(content: Content) -> Request {
    let mut r = Request { parts: Vec::new(), timestamp: chrono::Utc::now(), metadata: None };
    append_content(&mut r, content);
    r
}
