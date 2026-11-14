//! 别的领域的测试造会话数据用（feature `testing`，只在 dev-dependencies 里开，正式构建里没有这个模块）：会话的
//! 运行、信件、占用平时只由本领域自己写，这里按真实流程的同一串步骤写，测试不必知道里面的表和顺序。

use agenter::{Message, Request, RequestPart};
use chrono::Utc;
use muliao_platform::db::Tx;
use muliao_platform::errors::Result;
use muliao_platform::ids::{MailId, SessionId};

use crate::execution::actor::Waker;
use crate::execution::live::Live;
use crate::execution::runs::{Outcome, current_or_start_in, end_in};
use crate::intake::origins::Origin;
use crate::mailbox::{MailBody, NewMail, consume_in, discard_in, give_in, post_in, queued_in};
use crate::record::conventions::{PartKind, PartMark, attach_run_context, mark_parts};
use crate::record::history::append_in;

/// 一条请求：每段文字记下它所在信件的发送方（交出信件时就这样记），末尾带运行上下文。
pub fn user_request(texts: &[(Origin, &str)], run_context: &str) -> Message {
    let parts = texts.iter().map(|(_, t)| RequestPart::Text { text: (*t).into() }).collect();
    let mut request = Request { parts, timestamp: Utc::now(), metadata: None };
    let marks = texts
        .iter()
        .enumerate()
        .map(|(part, (origin, _))| PartMark { part, kind: PartKind::Message { origin: *origin } });
    mark_parts(&mut request, marks.collect());
    attach_run_context(&mut request, run_context);
    Message::User(request)
}

/// 一次运行：投一封 from 发来的消息（有的话）并交给运行，保存 messages，按 outcome 结束（None 为还在跑）。
pub fn record_run_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    session_id: &SessionId,
    from: Option<(Origin, String)>,
    messages: &[Message],
    outcome: Option<Outcome>,
) -> Result<()> {
    let mail = from
        .map(|(origin, content)| {
            let body = MailBody::Message { origin, content, attachments: Vec::new(), delegation_id: None };
            post_in(tx, waker, session_id, NewMail { body, awaited: true })
        })
        .transpose()?;
    // 测试不看实时展示：给它一条没人订阅的总线
    let live = Live::new(std::sync::Arc::new(muliao_platform::bus::Bus::new(16)));
    let run = current_or_start_in(tx, &live, session_id)?;
    if let Some(m) = mail {
        give_in(tx, &[m], run)?;
        consume_in(tx, &[m], run)?;
    }
    append_in(tx, session_id, run, messages)?;
    if let Some(o) = outcome {
        end_in(tx, &live, run, o)?;
    }
    Ok(())
}

/// 让会话空闲：丢掉排着的信。
pub fn discard_queued_in(tx: &mut Tx<'_>, session_id: &SessionId) -> Result<()> {
    let ids: Vec<MailId> = queued_in(tx, session_id)?.into_iter().map(|m| m.id).collect();
    discard_in(tx, session_id, &ids).map(drop)
}
