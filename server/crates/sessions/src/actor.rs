//! 会话 actor：每个会话一个协程，信箱里有要处理的信就跑一次运行，一次一个。没有任务队列（#675）：要不要跑只看库里的信、
//! 历史与运行记录（[`should_run`]，唯一规则），所以投信只需在提交后叫醒它（[`Waker`]），重启后扫一遍就能接着跑
//! （[`Actors::resume`]）。
//!
//! - 发给 actor 的命令：叫醒、停止、手动压缩、重试最终失败的运行。actor 没事可做时退出，下一条命令再起一个。
//! - 手动压缩只在没在跑时做，做完（或失败）才回复发起的用户；在跑时直接拒绝（运行中超阈值会自动压缩）。压缩中收到停止就
//!   丢掉它：没写完的这一批作废，已提交的批次保留。
//! - 运行失败：暂时性的（模型或外部服务一时不可用）退避重试，从同一个运行接着跑，约 30 分钟后算最终失败；其余当场最终失败：
//!   记在运行记录上（会话页显示错误与「重试」），结清交给它的委派，发布 RunFailed。
//! - 停止：在跑就丢掉这次运行（Rust 里丢掉即取消；数据库事务在写线程上，不会被打断一半），先存下它已产生的部分，再在同一个
//!   actor 里执行停止事务；不在跑就直接执行。返回时停止事务已提交。
//! - 到点唤醒与委派的运行中检查：一个节拍循环（[`Actors::start`]）。

use muliao_platform::ids::SessionId;
use std::collections::HashMap;
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex, Weak};

use futures::FutureExt;
use muliao_platform::db::Tx;
use muliao_platform::retry::Retry;
use tokio::sync::{mpsc, oneshot};

use crate::activity::{WorkState, work_state_in};
use crate::delegation::check_threads;
use crate::events::RunFailed;
use crate::halt::{Halted, StopThread, halt};
use crate::mailbox::{self, Mail};
use crate::pending::Position;
use crate::run::{Core, PartialSlot, execute, unsaved_of};
use crate::runs::{self, Outcome, RunRecord};
use crate::settlement::settle_failure_in;
use crate::work::{CheckBeats, TICK};
use crate::{directory, wakeups};
use muliao_platform::errors::{Error, Result, broken, conflict, not_found};

/// 叫醒会话 actor 的句柄：只说「这个会话可能有事了」，要不要跑由 actor 自己按 [`should_run`] 判断。可以先于 actor 系统
/// 构造（组合根里没有构造环）。
#[derive(Clone)]
pub struct Waker {
    tx: mpsc::UnboundedSender<SessionId>,
}

impl Waker {
    /// 一个叫醒句柄与它的接收端（交给 [`Actors::start`]）。
    pub fn channel() -> (Self, mpsc::UnboundedReceiver<SessionId>) {
        let (tx, rx) = mpsc::unbounded_channel();
        (Self { tx }, rx)
    }

    /// 这个事务提交后叫醒（回滚就不叫）。
    pub(crate) fn wake_on_commit(&self, tx: &mut Tx<'_>, session_id: &SessionId) {
        let (sender, id) = (self.tx.clone(), session_id.to_owned());
        tx.on_commit(move || {
            let _ = sender.send(id);
        });
    }
}

/// 唤醒规则（唯一一处）：会话没有没结束的运行时，要不要开始一次。信箱里有排着的信，并且不是这两种情况：
/// - 排着的信都已经交给过最近一次最终失败的运行：由它被手动重试、或之后新到的信唤醒的运行处理，不因同一批信反复失败；
/// - 停在挂起的调用上、还不能继续：等回答或新消息，事件等它们一起交给模型。
pub(crate) fn should_run(queued: &[Mail], latest: Option<&RunRecord>, at: &Position) -> bool {
    if queued.is_empty() {
        return false;
    }
    if let Some(run) = latest.filter(|r| r.failure().is_some())
        && queued.iter().all(|m| m.taken_by == Some(run.id))
    {
        return false;
    }
    !at.waiting_for_answers()
}

enum Command {
    Wake,
    Stop(oneshot::Sender<Result<Halted>>),
    Compact(oneshot::Sender<Result<usize>>),
    Retry(oneshot::Sender<Result<()>>),
}

struct Inner {
    core: Arc<Core>,
    actors: Mutex<HashMap<SessionId, mpsc::UnboundedSender<Command>>>,
}

/// 全部会话 actor。
#[derive(Clone)]
pub struct Actors {
    inner: Arc<Inner>,
}

impl Actors {
    /// 起叫醒的分发与节拍循环。之后调 [`Actors::resume`] 接着跑重启前没做完的。
    pub fn start(core: Arc<Core>, mut wakes: mpsc::UnboundedReceiver<SessionId>) -> Self {
        let actors = Actors { inner: Arc::new(Inner { core, actors: Mutex::default() }) };
        let weak = Arc::downgrade(&actors.inner);
        tokio::spawn(async move {
            while let Some(id) = wakes.recv().await {
                let Some(inner) = weak.upgrade() else { return };
                Actors { inner }.send(&id, Command::Wake);
            }
        });
        let weak = Arc::downgrade(&actors.inner);
        tokio::spawn(async move {
            let beats = Arc::new(Mutex::new(CheckBeats::default()));
            loop {
                tokio::time::sleep(TICK).await;
                let Some(inner) = weak.upgrade() else { return };
                let core = &inner.core;
                if let Err(e) = wakeups::deliver_due(&core.db, &core.waker, core.tz).await {
                    tracing::error!(error = %e, "delivering due wake-ups failed");
                }
                if let Err(e) = check_threads(&core.db, &core.waker, &beats).await {
                    tracing::error!(error = %e, "checking delegated threads failed");
                }
            }
        });
        actors
    }

    /// 服务启动时调用一次：有没结束的运行（停在半路）或有排着的信的会话各叫醒一次，按唤醒规则接着跑。
    pub async fn resume(&self) -> Result<usize> {
        let ids = self
            .inner
            .core
            .db
            .read(|c| {
                let mut ids = runs::sessions_unfinished_in(c)?;
                ids.extend(mailbox::sessions_with_queued_in(c)?);
                ids.sort();
                ids.dedup();
                Ok::<_, Error>(ids)
            })
            .await?;
        for id in &ids {
            self.send(id, Command::Wake);
        }
        tracing::info!(sessions = ids.len(), "session actors resumed");
        Ok(ids.len())
    }

    /// 停止会话：返回时停止事务已提交（之后投进来的信照常唤醒会话，不会被清掉）。
    pub(crate) async fn stop(&self, session_id: &SessionId) -> Result<Halted> {
        let (tx, rx) = oneshot::channel();
        self.send(session_id, Command::Stop(tx));
        rx.await.map_err(|_| broken("the session actor went away while stopping"))?
    }

    /// 手动压缩，做完才返回压缩的步数（0 是保留窗口之外没有可压缩的）。会话在跑、在压缩，或压缩中被停止时 Conflict。
    pub(crate) async fn compact(&self, session_id: &SessionId) -> Result<usize> {
        let (tx, rx) = oneshot::channel();
        self.send(session_id, Command::Compact(tx));
        rx.await.map_err(|_| broken("the session actor went away while compacting"))?
    }

    /// 重试最近一次最终失败的运行（同一个运行接着跑）。会话在跑、或最近一次运行没有失败时 Conflict。
    pub(crate) async fn retry(&self, session_id: &SessionId) -> Result<()> {
        let (tx, rx) = oneshot::channel();
        self.send(session_id, Command::Retry(tx));
        rx.await.map_err(|_| broken("the session actor went away while retrying"))?
    }

    /// 停下一个 thread 的回调（提交后调用，不等它）。
    fn stop_thread(inner: Weak<Inner>) -> StopThread {
        Arc::new(move |thread_id: SessionId| {
            let Some(inner) = inner.upgrade() else { return };
            tokio::spawn(async move {
                match (Actors { inner }).stop(&thread_id).await {
                    Ok(halted) => {
                        tracing::info!(session_id = %thread_id, ?halted, "thread of a stopped session stopped")
                    }
                    Err(e) => {
                        tracing::error!(session_id = %thread_id, error = %e, "stopping a thread of a stopped session failed")
                    }
                }
            });
        })
    }

    fn send(&self, session_id: &SessionId, cmd: Command) {
        let mut actors = self.inner.actors.lock().expect("actors lock");
        let cmd = match actors.get(session_id) {
            Some(tx) => match tx.send(cmd) {
                Ok(()) => return,
                Err(mpsc::error::SendError(cmd)) => cmd,
            },
            None => cmd,
        };
        let (tx, rx) = mpsc::unbounded_channel();
        tx.send(cmd).expect("receiver is alive");
        actors.insert(session_id.to_owned(), tx);
        let (inner, id) = (self.inner.clone(), session_id.to_owned());
        tokio::spawn(async move { actor(inner, id, rx).await });
    }
}

/// 一个会话的 actor：处理命令，有事就跑，没事就退出。
async fn actor(inner: Arc<Inner>, id: SessionId, mut rx: mpsc::UnboundedReceiver<Command>) {
    loop {
        while let Ok(cmd) = rx.try_recv() {
            idle_command(&inner, &id, cmd, &mut rx).await;
        }
        let idle = match has_work(&inner.core, &id).await {
            // 失败得连运行记录都没留下（开头的事务就出错）：不原地重来，等下一次叫醒
            Ok(true) => !run_with_retries(&inner, &id, &mut rx).await,
            Ok(false) => true,
            Err(e) => {
                tracing::error!(session_id = %id, error = %e, "session actor could not read its state");
                true
            }
        };
        if idle {
            // 在登记锁里确认没有新命令再退出（发命令的一方也在这把锁里发）；有排着的命令就回到主循环处理（处理完会再
            // 试一次，命令总会处理完，不会空转）
            let mut actors = inner.actors.lock().expect("actors lock");
            if rx.is_empty() {
                actors.remove(&id);
                return;
            }
        }
    }
}

/// 没在跑时收到的命令。
async fn idle_command(inner: &Arc<Inner>, id: &SessionId, cmd: Command, rx: &mut mpsc::UnboundedReceiver<Command>) {
    match cmd {
        Command::Wake => {}
        Command::Stop(reply) => {
            let halted = halt(&inner.core, id, None, Actors::stop_thread(Arc::downgrade(inner))).await;
            let _ = reply.send(halted);
        }
        Command::Compact(reply) => compact_until_stopped(inner, id, rx, reply).await,
        Command::Retry(reply) => {
            let _ = reply.send(reopen(&inner.core, id).await);
        }
    }
}

/// 有没结束的运行，或按唤醒规则该开始一次：就是工作状态的 running（同一条规则，删除是否拒绝也看它）。
async fn has_work(core: &Core, id: &SessionId) -> Result<bool> {
    let id = id.to_owned();
    core.db
        .read(move |c| {
            if directory::get_in(c, &id)?.is_none() {
                return Ok(false);
            }
            Ok(work_state_in(c, &id)? == WorkState::Running)
        })
        .await
}

/// 会话最后一条消息（判断一次尝试有没有往前推进）。读不出来时记下错误、当作没有：只影响退避要不要从头算。
async fn last_message(core: &Core, id: &SessionId) -> Option<i64> {
    let session_id = id.to_owned();
    match core.db.read(move |c| crate::history::last_message_id_in(c, &session_id)).await {
        Ok(last) => last,
        Err(e) => {
            tracing::error!(session_id = %id, error = %e, "reading the last message of the session failed");
            None
        }
    }
}

/// 运行一次（失败时按规则重试），其间收到停止就丢掉它、执行停止事务。返回 false：最终失败却没有运行记录可记（开头
/// 的事务就出错），立即再跑只会再错一遍。
async fn run_with_retries(inner: &Arc<Inner>, id: &SessionId, rx: &mut mpsc::UnboundedReceiver<Command>) -> bool {
    let core = &inner.core;
    // 结束了（成功、最终失败或停止）就不再显示在等重试
    let attempts = core.live.attempts(id);
    // 暂时性失败按平台的重试节奏重试；一次尝试往前推进了（存下了新消息）就从头算
    let mut backoff = Retry::default().start();
    loop {
        let before = last_message(core, id).await;
        let partial: PartialSlot = Arc::default();
        let outcome = {
            let run = AssertUnwindSafe(execute(core, id, &partial)).catch_unwind();
            tokio::pin!(run);
            loop {
                tokio::select! {
                    r = &mut run => break Ok(r),
                    cmd = rx.recv() => match cmd {
                        Some(Command::Stop(reply)) => break Err(reply),
                        Some(Command::Compact(reply)) => refuse_compaction_while_running(reply),
                        Some(Command::Retry(reply)) => { let _ = reply.send(Err(conflict("the session is running"))); }
                        Some(Command::Wake) | None => {}
                    }
                }
            }
            // 离开这个块时运行被丢掉（停止时）
        };
        let failure = match outcome {
            Err(reply) => {
                // 等排在前面的写事务做完（写线程先进先出）：被丢掉的运行如果正等着保存一步，那一步先提交并取走对应的已产生
                // 部分，这里才取剩下的，不会存两遍
                if let Err(e) = core.db.write(|_| Ok::<_, rusqlite::Error>(())).await {
                    let _ = reply.send(Err(e.into()));
                    return true;
                }
                let halted = match unsaved_of(&partial).await {
                    Ok(unsaved) => halt(core, id, unsaved, Actors::stop_thread(Arc::downgrade(inner))).await,
                    Err(e) => Err(e),
                };
                let _ = reply.send(halted);
                return true;
            }
            Ok(Ok(Ok(()))) => return true,
            Ok(Ok(Err(f))) => f,
            Ok(Err(panic)) => {
                let message = panic
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| panic.downcast_ref::<&str>().map(|s| (*s).to_owned()))
                    .unwrap_or_else(|| "the run panicked".to_owned());
                crate::run::Failure::Final(format!("panicked: {message}"))
            }
        };
        if last_message(core, id).await != before {
            backoff.reset();
        }
        if let crate::run::Failure::Transient(message) = &failure
            && let Some(delay) = backoff.next_delay()
        {
            tracing::warn!(session_id = %id, error = %message, retry_in = ?delay, "run failed transiently; retrying");
            attempts.retrying(message, delay);
            // 退避期间也能停止（停在两次尝试之间：没有已产生、没保存的部分）
            let sleep = tokio::time::sleep(delay);
            tokio::pin!(sleep);
            loop {
                tokio::select! {
                    _ = &mut sleep => break,
                    cmd = rx.recv() => match cmd {
                        Some(Command::Stop(reply)) => {
                            let halted = halt(core, id, None, Actors::stop_thread(Arc::downgrade(inner))).await;
                            let _ = reply.send(halted);
                            return true;
                        }
                        Some(Command::Compact(reply)) => refuse_compaction_while_running(reply),
                        Some(Command::Retry(reply)) => { let _ = reply.send(Err(conflict("the session is running"))); }
                        Some(Command::Wake) | None => {}
                    }
                }
            }
            continue;
        }
        return fail(core, id, failure.message()).await;
    }
}

/// 最终失败：记在运行记录上，结清交给它的委派，发布 RunFailed。这个收尾自己出错时（如订阅者出 bug）只记下失败，不带
/// 收尾：委派不结清，发起方的运行中检查会如实说，由它停掉 thread。返回有没有运行记录可记。
async fn fail(core: &Core, id: &SessionId, error: &str) -> bool {
    tracing::error!(session_id = %id, error, "run failed");
    let (session_id, message, waker, events, live) =
        (id.to_owned(), error.to_owned(), core.waker.clone(), core.events.clone(), core.live.clone());
    let full = core
        .db
        .write(move |tx| {
            let Some(run) = runs::unfinished_in(tx, &session_id)? else { return Ok(false) };
            runs::end_in(tx, &live, run.id, Outcome::Failed(message.clone()))?;
            let session = directory::running_in(tx, &session_id)?;
            settle_failure_in(tx, &waker, &session, run.id, &message)?;
            let agent_name = muliao_assets::agents::agent_in(tx, &session.agent_id)?.name;
            events.run_failed(
                tx,
                &RunFailed {
                    session_id: session.id.clone(),
                    agent_id: session.agent_id.clone(),
                    agent_name: agent_name.to_string(),
                    run_id: run.id,
                    origin: session.origin,
                    error: message.clone(),
                },
            )?;
            // 运行开始之后才到的信（没交给过它）唤醒新的运行
            waker.wake_on_commit(tx, &session_id);
            Ok::<_, Error>(true)
        })
        .await;
    if let Ok(recorded) = full {
        return recorded;
    }
    if let Err(e) = full {
        tracing::error!(session_id = %id, error = %e, "settling a failed run failed; recording the failure alone");
        let (session_id, message, live) = (id.to_owned(), error.to_owned(), core.live.clone());
        let alone = core
            .db
            .write(move |tx| match runs::unfinished_in(tx, &session_id)? {
                Some(run) => runs::end_in(tx, &live, run.id, Outcome::Failed(message)).map(|()| true),
                None => Ok(false),
            })
            .await;
        match alone {
            Ok(recorded) => return recorded,
            Err(e) => tracing::error!(session_id = %id, error = %e, "recording a failed run failed"),
        }
    }
    false
}

/// 手动重试：最近一次运行最终失败、没有在跑的运行时，把它重新算作没结束（actor 随后接着跑）。
async fn reopen(core: &Core, id: &SessionId) -> Result<()> {
    let (id, live) = (id.to_owned(), core.live.clone());
    core.db
        .write(move |tx| {
            directory::get_in(tx, &id)?.ok_or_else(|| not_found(format!("session {id:?} not found")))?;
            if runs::unfinished_in(tx, &id)?.is_some() {
                return Err(conflict("the session is running"));
            }
            match runs::latest_in(tx, &id)? {
                Some(r) if r.failure().is_some() => runs::reopen_in(tx, &live, &r),
                _ => Err(conflict("the latest run of this session did not fail; nothing to retry")),
            }
        })
        .await
}

fn refuse_compaction_while_running(reply: oneshot::Sender<Result<usize>>) {
    let _ = reply.send(Err(conflict(
        "the session is running; it compacts automatically when its context grows over the threshold, \
         compact by hand after the run ends",
    )));
}

/// 没在跑时的手动压缩，期间收到停止就丢掉它（没提交的这一批作废；写事务在写线程上，不会被打断一半），回复发起压缩的一方
/// 「已停止」，再照常执行停止事务。
async fn compact_until_stopped(
    inner: &Arc<Inner>,
    id: &SessionId,
    rx: &mut mpsc::UnboundedReceiver<Command>,
    reply: oneshot::Sender<Result<usize>>,
) {
    let stop = {
        let compaction = compact(&inner.core, id);
        tokio::pin!(compaction);
        loop {
            tokio::select! {
                r = &mut compaction => {
                    let _ = reply.send(r);
                    return;
                }
                cmd = rx.recv() => match cmd {
                    Some(Command::Stop(stop)) => break stop,
                    Some(Command::Compact(other)) => { let _ = other.send(Err(conflict("the session is already compacting"))); }
                    Some(Command::Retry(other)) => { let _ = other.send(Err(conflict("the session is compacting"))); }
                    Some(Command::Wake) | None => {}
                }
            }
        }
        // 离开这个块时压缩被丢掉
    };
    tracing::info!(session_id = %id, "manual compaction stopped");
    let _ = reply.send(Err(conflict("compaction stopped")));
    let halted = halt(&inner.core, id, None, Actors::stop_thread(Arc::downgrade(inner))).await;
    let _ = stop.send(halted);
}

/// 手动压缩（没在跑时）：结果（含失败）交回发起的用户，不另记运行记录。
async fn compact(core: &Core, id: &SessionId) -> Result<usize> {
    let session_id = id.to_owned();
    let agent_id = core
        .db
        .read(move |c| {
            let session = directory::get_in(c, &session_id)?
                .ok_or_else(|| not_found(format!("session {session_id:?} not found")))?;
            // 正在删除的 agent 的会话不再新排工作（与来信同一条规则）
            muliao_assets::agents::active_agent_in(c, &session.agent_id)?;
            Ok::<_, Error>(session.agent_id)
        })
        .await?;
    let result = async {
        let assets = core.runtime.run_assets(&agent_id).await?;
        core.compactor.compact(id, &assets).await
    }
    .await;
    match &result {
        Ok(0) => tracing::info!(session_id = %id, "nothing to compact beyond the kept steps"),
        Ok(n) => tracing::info!(session_id = %id, steps = n, "session compacted by the user"),
        Err(e) => tracing::error!(session_id = %id, error = %e, "manual compaction failed"),
    }
    result
}
