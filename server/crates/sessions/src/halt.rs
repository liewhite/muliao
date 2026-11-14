//! 停止会话（docs/architecture.md「停止会话」）：一个事务，只写会话自己的数据。运行中被停止时由 actor 在这次运行看到停止
//! 信号、自己停下之后执行（先存下它交出的已产生部分），没在运行时直接执行。
//!
//! 1. 让库里的历史停在不会被续跑的地方：悬空的工具调用补上「被停止」的结果（等用户回答的提问除外：回答已提交的写上回答，
//!    没回答的留着，用户仍可回答），末尾没得到回复的请求、生成到一半的回复标上停止。
//! 2. 没结束的运行记为停止。
//! 3. 全部 active 占用记为 cleared：之后才到的结局（后台任务、委派）一律不投。
//! 4. 交给这个会话、还没结清的委派结清为「被停止」，交回发起方；清空信箱。
//! 5. 被清掉的委派：做这些工作的 thread 在提交后各自停下（逐层，由它们自己的停止继续）。服务恰好在这之间重启时它们不会被停，
//!    跑完的结局因占用已清掉而不投（罕见、只多花一段 token，不为它持久化停止任务）。
//! 6. 发布 SessionStopped：工作区领域在提交后结束被清掉的后台任务。

use muliao_platform::ids::{DelegationId, JobId, RunId, SessionId};
use std::collections::BTreeSet;
use std::sync::Arc;

use agenter::Message;
use muliao_platform::db::Tx;

use crate::conventions::{MadeUp, is_interrupted, is_stopped, made_up_result, mark_stopped, results_request};
use crate::directory;
use crate::events::SessionStopped;
use crate::history::{self, since_last_response};
use crate::holds::{HoldOf, clear_in};
use crate::mailbox::consume_in;
use crate::pending::closing_on_stop;
use crate::run::Core;
use crate::runs;
use crate::settlement::settle_stopped_in;
use crate::thread_calls::delegations_among;
use crate::work::position_in;
use muliao_platform::errors::{Result, broken};

/// 停止的结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Halted {
    Stopped,
    /// 会话已删除（例如逐层停止一个刚被用户删掉的 thread）：契约内的情况，不是错误
    Gone,
}

/// 停下一个 thread（提交后调用；由 actor 系统给）。
pub(crate) type StopThread = Arc<dyn Fn(SessionId) + Send + Sync>;

/// 停止事务。unsaved：运行中被停止时这次运行已产生、还没保存的消息，记在还没结束的那次运行名下、存在最前面。
pub(crate) async fn halt(
    core: &Core,
    session_id: &SessionId,
    mut unsaved: Vec<Message>,
    stop_thread: StopThread,
) -> Result<Halted> {
    core.redaction.messages(&mut unsaved);
    let (session_id, waker, events, live) =
        (session_id.to_owned(), core.waker.clone(), core.events.clone(), core.live.clone());
    core.db
        .write(move |tx| {
            let Some(session) = directory::get_in(tx, &session_id)? else {
                tracing::info!(%session_id, "stopping a deleted session: nothing to do");
                return Ok(Halted::Gone);
            };
            // 存下的和补上的消息都记在最近一次运行名下（被停的运行，或调用挂起时的那次）
            let latest = runs::latest_in(tx, &session_id)?.map(|r| r.id);
            if !unsaved.is_empty() {
                history::append_in(tx, &session_id, run_of(&session_id, latest)?, &unsaved)?;
            }
            close_history_in(tx, &session_id, latest)?;
            let stopped_runs = runs::stop_unfinished_in(tx, &live, &session_id)?;
            // 没在跑的会话（在等后台任务、委派、唤醒）没有运行记录可改，可停止照样改了它看得见的内容（补上的结果、清掉的
            // 等待项、丢弃的信）：看着它的页面也要重拉
            live.saved_on_commit(tx, &session_id);
            let cleared = clear_in(tx, &session_id)?;
            let answer = history::latest_answer(tx, &session_id)?;
            let (settled, discarded) = settle_stopped_in(tx, &waker, &session, &answer)?;
            let rows: Vec<DelegationId> = cleared
                .iter()
                .filter_map(|h| match h.of {
                    HoldOf::Delegation { row } => Some(row),
                    _ => None,
                })
                .collect();
            let delegations = delegations_among(tx, &rows)?;
            let threads: Vec<SessionId> =
                delegations.values().map(|d| d.thread_id.clone()).collect::<BTreeSet<_>>().into_iter().collect();
            for thread in threads.clone() {
                let stop = stop_thread.clone();
                tx.on_commit(move || stop(thread));
            }
            let jobs: Vec<JobId> = cleared
                .iter()
                .filter_map(|h| match h.of {
                    HoldOf::Job { job_id } => Some(job_id),
                    _ => None,
                })
                .collect();
            events.session_stopped(tx, &SessionStopped { session_id: session_id.clone(), jobs })?;
            tracing::info!(%session_id, stopped_runs = ?stopped_runs, discarded_mail = ?discarded,
                cleared_holds = ?cleared.iter().map(|h| h.id).collect::<Vec<_>>(), settled_delegations = ?settled,
                stopping_threads = ?threads, "session halted");
            Ok(Halted::Stopped)
        })
        .await
}

/// 停止时要写进历史的消息记在哪次运行名下：有这样的消息，会话就一定跑过。
fn run_of(session_id: &SessionId, latest: Option<RunId>) -> Result<RunId> {
    latest.ok_or_else(|| broken(format!("session {session_id} has messages to close but no run")))
}

/// 让会话历史停在不会被续跑的地方：哪些调用补「已停止」、哪些写上已提交的回答按会话停在哪定，用上的回答信件随之记为已处理。
fn close_history_in(tx: &mut Tx<'_>, session_id: &SessionId, latest: Option<RunId>) -> Result<()> {
    let tail = since_last_response(tx, session_id)?;
    let closing = closing_on_stop(&position_in(tx, session_id)?);
    let mut results: Vec<_> = closing.stopped.iter().map(|c| made_up_result(c, MadeUp::Stopped)).collect();
    results.extend(closing.answered);
    if let Some(request) = results_request(results) {
        let run_id = run_of(session_id, latest)?;
        let mut message = Message::User(request);
        mark_stopped(&mut message);
        history::append_in(tx, session_id, run_id, &[message])?;
        consume_in(tx, &closing.used, run_id)?;
        return Ok(());
    }
    // 没得到回复的请求，或生成到一半被打断的回复：标上停止。完整的回复有意不标（它确实是完整的；空闲会话被停止时也停在它上）
    if let Some(last) = tail.last()
        && !is_stopped(last)
    {
        let mark = match last {
            Message::User(_) => true,
            Message::Assistant(r) => is_interrupted(r),
        };
        if mark {
            history::mark_last_stopped_in(tx, session_id)?;
        }
    }
    Ok(())
}
