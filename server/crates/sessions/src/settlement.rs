//! 结清：会话自己判断委派来的工作（带委派的信件）什么时候结束，结局在同一个事务里交回发起方（[`crate::delegation`]）。
//! 时机只有这几个：运行正常结束且工作做完（[`settle_finished_in`]）、运行最终失败、停止、删除。一项委派结清没有只看发起方
//! 引用它的占用是否还 active：交回结局就释放占用，所以每项委派只交回一次。

use std::collections::BTreeSet;

use muliao_platform::db::Tx;
use muliao_runtime::conventions::SharedFile;
use rusqlite::Connection;

use crate::actor::Waker;
use crate::delegation::{Ending, hand_back_in};
use crate::directory::Session;
use crate::mailbox::{Mail, MailBody, MailState, delegated_mail_in, discard_in, queued_in};
use crate::thread_calls::{Owed, owed_in};
use muliao_platform::errors::Result;

/// 交给会话、还没做完的信件所在的状态：排着的与已交给模型的
const LIVE: [MailState; 2] = [MailState::Queued, MailState::Consumed];

fn owed_by(c: &Connection, mail: &[Mail]) -> Result<Vec<Owed>> {
    let ids: Vec<i64> = mail
        .iter()
        .filter_map(|m| match m.body {
            MailBody::Message { delegation_id, .. } => delegation_id,
            _ => None,
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    owed_in(c, Some(&ids), None)
}

/// 会话有委派来、还没结清的工作（排着的或已交给模型的委派信件，发起方的占用还 active）：回答交给谁据此定。
pub(crate) fn delegated_in(c: &Connection, session_id: &str) -> Result<bool> {
    Ok(!owed_by(c, &delegated_mail_in(c, session_id, &LIVE)?)?.is_empty())
}

fn settle_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    session: &Session,
    mail: &[Mail],
    ending: Ending,
    text: &str,
    attachments: &[SharedFile],
) -> Result<Vec<i64>> {
    let owed = owed_by(tx, mail)?;
    if owed.is_empty() {
        return Ok(Vec::new());
    }
    let settled: Vec<i64> = owed.iter().map(|o| o.delegation.id).collect();
    tracing::info!(session_id = %session.id, ending = ?ending, delegations = ?settled, "session settled its delegations");
    hand_back_in(tx, waker, session, &owed, ending, text, attachments)?;
    Ok(settled)
}

/// 运行正常结束且工作做完：已交给模型、还没结清的委派以「完成」结清，带上回答与附件。委派可能跨几次运行：停在挂起的调用
/// 上、或还有工作没做完时，由之后那次做完的运行结清。
pub(crate) fn settle_finished_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    session: &Session,
    answer: &str,
    attachments: &[SharedFile],
) -> Result<Vec<i64>> {
    let mail = delegated_mail_in(tx, &session.id, &[MailState::Consumed])?;
    settle_in(tx, waker, session, &mail, Ending::Finished, answer, attachments)
}

/// 用户停止：交给这个会话、还没结清的委派以「停止」结清，带上最近的回答；然后清空信箱。返回结清的委派与丢弃的信件。
pub(crate) fn settle_stopped_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    session: &Session,
    answer: &str,
) -> Result<(Vec<i64>, Vec<i64>)> {
    let mail = delegated_mail_in(tx, &session.id, &LIVE)?;
    let settled = settle_in(tx, waker, session, &mail, Ending::Stopped, answer, &[])?;
    let queued: Vec<i64> = queued_in(tx, &session.id)?.iter().map(|m| m.id).collect();
    Ok((settled, discard_in(tx, &session.id, &queued)?))
}

/// 会话被删除：交给它、还没结清的委派以「被删除」结清。
pub(crate) fn settle_deleted_in(tx: &mut Tx<'_>, waker: &Waker, session: &Session) -> Result<Vec<i64>> {
    let mail = delegated_mail_in(tx, &session.id, &LIVE)?;
    settle_in(tx, waker, session, &mail, Ending::Deleted, "", &[])
}

/// 运行最终失败：交给这个会话、没能做完的委派以「失败」结清（发起方不会一直等）。按会话范围：已交给模型、还没结清的全部，
/// 加上交给过这次运行、还排着的（运行在第一次保存之前就失败时，交给它的信一直排着，也不会再有运行因为它们被唤醒）；
/// 它们随结清一起退出信箱。运行开始之后才到的不动：它们会唤醒新的运行。
pub(crate) fn settle_failure_in(
    tx: &mut Tx<'_>,
    waker: &Waker,
    session: &Session,
    run_id: i64,
    error: &str,
) -> Result<Vec<i64>> {
    let queued: Vec<Mail> = delegated_mail_in(tx, &session.id, &[MailState::Queued])?
        .into_iter()
        .filter(|m| m.taken_by == Some(run_id))
        .collect();
    let mut mail = delegated_mail_in(tx, &session.id, &[MailState::Consumed])?;
    mail.extend(queued.iter().cloned());
    let settled = settle_in(tx, waker, session, &mail, Ending::Failed, error, &[])?;
    // 交给它的委派信件都退出信箱，不论这次是否交回了结局：已结清的（发起方被停止）也没人再要这项工作
    discard_in(tx, &session.id, &queued.iter().map(|m| m.id).collect::<Vec<_>>())?;
    Ok(settled)
}
