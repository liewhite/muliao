//! 会话 actor：每个会话一个协程，信箱里有要处理的信就跑一次运行，一次一个。没有任务队列（#675）：要不要跑只看库里的信、
//! 历史与运行记录（[`should_run`]，唯一规则），所以投信只需在提交后叫醒它（[`Waker`]），重启后扫一遍就能接着跑
//! （[`Actors::resume`]）。
//!
//! - 发给 actor 的命令：叫醒、停止、手动压缩、重试最终失败的运行。actor 没事可做时退出，下一条命令再起一个。
//! - 运行失败：暂时性的（模型或外部服务一时不可用）退避重试，从同一个运行接着跑，约 30 分钟后算最终失败；其余当场最终失败：
//!   记在运行记录上（会话页显示错误与「重试」），结清交给它的委派，发布 RunFailed。
//! - 停止：在跑就丢掉这次运行（Rust 里丢掉即取消；数据库事务在写线程上，不会被打断一半），先存下它已产生的部分，再在同一个
//!   actor 里执行停止事务；不在跑就直接执行。返回时停止事务已提交。
//! - 到点唤醒与委派的运行中检查：一个节拍循环（[`Actors::start`]）。

use std::collections::HashMap;
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex, Weak};
use std::time::{Duration, Instant};

use futures::FutureExt;
use muliao_platform::db::Tx;
use tokio::sync::{mpsc, oneshot};

use crate::activity::{WorkState, work_state_in};
use crate::delegation::check_threads;
use crate::error::{Error, Result, broken, conflict, not_found};
use crate::events::RunFailed;
use crate::halt::{Halted, StopThread, halt};
use crate::mailbox::Mail;
use crate::pending::Position;
use crate::run::{Core, Partial, SharedPartial, execute, unsaved_of};
use crate::runs::{self, Outcome, RunKind, RunRecord};
use crate::settlement::settle_failure_in;
use crate::work::CheckBeats;
use crate::{directory, wakeups};

/// 叫醒会话 actor 的句柄：只说「这个会话可能有事了」，要不要跑由 actor 自己按 [`should_run`] 判断。可以先于 actor 系统
/// 构造（组合根里没有构造环）。
#[derive(Clone)]
pub struct Waker {
    tx: mpsc::UnboundedSender<String>,
}

impl Waker {
    /// 一个叫醒句柄与它的接收端（交给 [`Actors::start`]）。
    pub fn channel() -> (Self, mpsc::UnboundedReceiver<String>) {
        let (tx, rx) = mpsc::unbounded_channel();
        (Self { tx }, rx)
    }

    /// 现在叫醒。
    pub fn wake(&self, session_id: &str) {
        // 接收端不在了：服务在关停
        let _ = self.tx.send(session_id.to_owned());
    }

    /// 这个事务提交后叫醒（回滚就不叫）。
    pub fn wake_on_commit(&self, tx: &mut Tx<'_>, session_id: &str) {
        let (sender, id) = (self.tx.clone(), session_id.to_owned());
        tx.on_commit(move || {
            let _ = sender.send(id);
        });
    }
}

/// 唤醒规则（唯一一处）：会话没有没结束的运行时，要不要开始一次。信箱里有排着的信，并且不是这两种情况：
/// - 排着的信都已经交给过最近一次最终失败的运行：由它被手动重试、或之后新到的信唤醒的运行处理，不因同一批信反复失败；
/// - 停在挂起的调用上、还不能继续：等回答或新消息，事件等它们一起交给模型。
pub fn should_run(queued: &[Mail], latest: Option<&RunRecord>, at: &Position) -> bool {
    if queued.is_empty() {
        return false;
    }
    if let Some(run) = latest.filter(|r| r.failed())
        && queued.iter().all(|m| m.taken_by == Some(run.id))
    {
        return false;
    }
    !at.waiting_for_answers()
}

/// 暂时性失败的重试：第一次失败起这么久之后还不行就算最终失败（中转站与跨境 HTTPS 断过十几到三十分钟）。
const RETRY_WINDOW: Duration = Duration::from_secs(30 * 60);
const FIRST_DELAY: Duration = Duration::from_secs(5);
const MAX_DELAY: Duration = Duration::from_secs(300);
/// 到点唤醒与委派运行中检查的节拍
const TICK: Duration = Duration::from_secs(5);

enum Command {
    Wake,
    Stop(oneshot::Sender<Result<Halted>>),
    Compact(oneshot::Sender<Result<usize>>),
    Retry(oneshot::Sender<Result<()>>),
}

struct Inner {
    core: Arc<Core>,
    actors: Mutex<HashMap<String, mpsc::UnboundedSender<Command>>>,
}

/// 全部会话 actor。
#[derive(Clone)]
pub struct Actors {
    inner: Arc<Inner>,
}

impl Actors {
    /// 起叫醒的分发与节拍循环。之后调 [`Actors::resume`] 接着跑重启前没做完的。
    pub fn start(core: Arc<Core>, mut wakes: mpsc::UnboundedReceiver<String>) -> Self {
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

    /// 服务启动时、对外服务之前调用一次：有没结束的运行（停在半路）或有排着的信的会话各叫醒一次，按唤醒规则接着跑；停在
    /// 半路的手动压缩记为失败（之后才开始的压缩会被误记，所以必须先于对外服务）。
    pub async fn resume(&self) -> Result<usize> {
        let abandoned = self.inner.core.db.write(|tx| runs::abandon_compactions_in(tx)).await?;
        if abandoned > 0 {
            tracing::warn!(compactions = abandoned, "manual compactions interrupted by a restart were dropped");
        }
        let ids = self
            .inner
            .core
            .db
            .read(|c| {
                let mut ids = runs::sessions_unfinished_in(c)?;
                let mut stmt = c.prepare("SELECT DISTINCT session_id FROM mail WHERE state = 'queued'")?;
                let queued: Vec<String> = stmt.query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
                ids.extend(queued);
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
    pub async fn stop(&self, session_id: &str) -> Result<Halted> {
        let (tx, rx) = oneshot::channel();
        self.send(session_id, Command::Stop(tx));
        rx.await.map_err(|_| broken("the session actor went away while stopping"))?
    }

    /// 手动压缩：排在当前运行之后。返回压缩的步数（0 是保留窗口之外没有可压缩的）。
    pub async fn compact(&self, session_id: &str) -> Result<usize> {
        let (tx, rx) = oneshot::channel();
        self.send(session_id, Command::Compact(tx));
        rx.await.map_err(|_| broken("the session actor went away while compacting"))?
    }

    /// 重试最近一次最终失败的运行（同一个运行接着跑）。会话在跑、或最近一次运行没有失败时 Conflict。
    pub async fn retry(&self, session_id: &str) -> Result<()> {
        let (tx, rx) = oneshot::channel();
        self.send(session_id, Command::Retry(tx));
        rx.await.map_err(|_| broken("the session actor went away while retrying"))?
    }

    /// 停下一个 thread 的回调（提交后调用，不等它）。
    fn stop_thread(inner: Weak<Inner>) -> StopThread {
        Arc::new(move |thread_id: String| {
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

    fn send(&self, session_id: &str, cmd: Command) {
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
async fn actor(inner: Arc<Inner>, id: String, mut rx: mpsc::UnboundedReceiver<Command>) {
    let mut compactions: Vec<oneshot::Sender<Result<usize>>> = Vec::new();
    loop {
        while let Ok(cmd) = rx.try_recv() {
            idle_command(&inner, &id, cmd, &mut compactions).await;
        }
        if let Some(reply) = compactions.pop() {
            let _ = reply.send(compact(&inner.core, &id).await);
            continue;
        }
        match has_work(&inner.core, &id).await {
            Ok(true) => {
                if !run_with_retries(&inner, &id, &mut rx, &mut compactions).await {
                    // 失败得连运行记录都没留下（开头的事务就出错）：不原地重来，等下一次叫醒；有排着的命令就回到主循环处理
                    // （处理完会再试一次，命令总会处理完，不会空转）
                    let mut actors = inner.actors.lock().expect("actors lock");
                    if rx.is_empty() {
                        actors.remove(&id);
                        return;
                    }
                }
            }
            Ok(false) => {
                // 没事可做：在登记锁里确认没有新命令再退出（发命令的一方也在这把锁里发）
                let mut actors = inner.actors.lock().expect("actors lock");
                if rx.is_empty() {
                    actors.remove(&id);
                    return;
                }
            }
            Err(e) => {
                tracing::error!(session_id = %id, error = %e, "session actor could not read its state");
                let mut actors = inner.actors.lock().expect("actors lock");
                if rx.is_empty() {
                    actors.remove(&id);
                    return;
                }
            }
        }
    }
}

/// 没在跑时收到的命令。
async fn idle_command(
    inner: &Arc<Inner>,
    id: &str,
    cmd: Command,
    compactions: &mut Vec<oneshot::Sender<Result<usize>>>,
) {
    match cmd {
        Command::Wake => {}
        Command::Stop(reply) => {
            let halted = halt(&inner.core, id, (None, Vec::new()), Actors::stop_thread(Arc::downgrade(inner))).await;
            let _ = reply.send(halted);
        }
        Command::Compact(reply) => compactions.push(reply),
        Command::Retry(reply) => {
            let _ = reply.send(reopen(&inner.core, id).await);
        }
    }
}

/// 有没结束的运行，或按唤醒规则该开始一次：就是工作状态的 running（同一条规则，删除是否拒绝也看它）。
async fn has_work(core: &Core, id: &str) -> Result<bool> {
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

/// 会话最后一条消息（判断一次尝试有没有往前推进）。
async fn last_message(core: &Core, id: &str) -> Option<i64> {
    let id = id.to_owned();
    core.db.read(move |c| crate::history::last_message_id_in(c, &id)).await.ok().flatten()
}

/// 运行一次（失败时按规则重试），其间收到停止就丢掉它、执行停止事务。返回 false：最终失败却没有运行记录可记（开头
/// 的事务就出错），立即再跑只会再错一遍。
async fn run_with_retries(
    inner: &Arc<Inner>,
    id: &str,
    rx: &mut mpsc::UnboundedReceiver<Command>,
    compactions: &mut Vec<oneshot::Sender<Result<usize>>>,
) -> bool {
    let core = &inner.core;
    // 重试窗口从第一次失败算起；一次尝试往前推进了（存下了新消息）就重新算
    let mut first_failure: Option<Instant> = None;
    let mut delay = FIRST_DELAY;
    loop {
        let before = last_message(core, id).await;
        let partial: SharedPartial = Arc::new(Mutex::new(Partial::default()));
        let outcome = {
            let run = AssertUnwindSafe(execute(core, id, &partial)).catch_unwind();
            tokio::pin!(run);
            loop {
                tokio::select! {
                    r = &mut run => break Ok(r),
                    cmd = rx.recv() => match cmd {
                        Some(Command::Stop(reply)) => break Err(reply),
                        Some(Command::Compact(reply)) => compactions.push(reply),
                        Some(Command::Retry(reply)) => { let _ = reply.send(Err(conflict("the session is running"))); }
                        Some(Command::Wake) | None => {}
                    }
                }
            }
            // 离开这个块时运行被丢掉（停止时）
        };
        let failure = match outcome {
            Err(reply) => {
                core.live.end(id);
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
                core.live.end(id);
                let message = panic
                    .downcast_ref::<String>()
                    .cloned()
                    .or_else(|| panic.downcast_ref::<&str>().map(|s| (*s).to_owned()))
                    .unwrap_or_else(|| "the run panicked".to_owned());
                crate::run::Failure { transient: false, message: format!("panicked: {message}") }
            }
        };
        if last_message(core, id).await != before {
            first_failure = None;
            delay = FIRST_DELAY;
        }
        let since = *first_failure.get_or_insert_with(Instant::now);
        if failure.transient && since.elapsed() < RETRY_WINDOW {
            tracing::warn!(session_id = id, error = %failure.message, retry_in = ?delay, "run failed transiently; retrying");
            // 退避期间也能停止（停在两次尝试之间：没有已产生、没保存的部分）
            let sleep = tokio::time::sleep(delay);
            tokio::pin!(sleep);
            loop {
                tokio::select! {
                    _ = &mut sleep => break,
                    cmd = rx.recv() => match cmd {
                        Some(Command::Stop(reply)) => {
                            let halted = halt(core, id, (None, Vec::new()), Actors::stop_thread(Arc::downgrade(inner))).await;
                            let _ = reply.send(halted);
                            return true;
                        }
                        Some(Command::Compact(reply)) => compactions.push(reply),
                        Some(Command::Retry(reply)) => { let _ = reply.send(Err(conflict("the session is running"))); }
                        Some(Command::Wake) | None => {}
                    }
                }
            }
            delay = (delay * 2).min(MAX_DELAY);
            continue;
        }
        return fail(core, id, &failure.message).await;
    }
}

/// 最终失败：记在运行记录上，结清交给它的委派，发布 RunFailed。这个收尾自己出错时（如订阅者出 bug）只记下失败，不带
/// 收尾：委派不结清，发起方的运行中检查会如实说，由它停掉 thread。返回有没有运行记录可记。
async fn fail(core: &Core, id: &str, error: &str) -> bool {
    tracing::error!(session_id = id, error, "run failed");
    let (session_id, message, waker, events, live) =
        (id.to_owned(), error.to_owned(), core.waker.clone(), core.events.clone(), core.live.clone());
    let full = core
        .db
        .write(move |tx| {
            let Some(run) = runs::unfinished_in(tx, &session_id)? else { return Ok(false) };
            runs::end_in(tx, &live, run.id, Outcome::Failed, Some(&message))?;
            let session = directory::get_in(tx, &session_id)?
                .ok_or_else(|| not_found(format!("session {session_id} not found")))?;
            settle_failure_in(tx, &waker, &session, run.id, &message)?;
            let agent_name = muliao_assets::agents::agent_in(tx, &session.agent_id)?.name;
            events.run_failed(
                tx,
                &RunFailed {
                    session_id: session.id.clone(),
                    agent_id: session.agent_id.clone(),
                    agent_name,
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
        tracing::error!(session_id = id, error = %e, "settling a failed run failed; recording the failure alone");
        let (session_id, message, live) = (id.to_owned(), error.to_owned(), core.live.clone());
        let alone = core
            .db
            .write(move |tx| match runs::unfinished_in(tx, &session_id)? {
                Some(run) => runs::end_in(tx, &live, run.id, Outcome::Failed, Some(&message)).map(|()| true),
                None => Ok(false),
            })
            .await;
        match alone {
            Ok(recorded) => return recorded,
            Err(e) => tracing::error!(session_id = id, error = %e, "recording a failed run failed"),
        }
    }
    false
}

/// 手动重试：最近一次运行最终失败、没有在跑的运行时，把它重新算作没结束（actor 随后接着跑）。
async fn reopen(core: &Core, id: &str) -> Result<()> {
    let (id, live) = (id.to_owned(), core.live.clone());
    core.db
        .write(move |tx| {
            directory::get_in(tx, &id)?.ok_or_else(|| not_found(format!("session {id:?} not found")))?;
            if runs::unfinished_in(tx, &id)?.is_some() {
                return Err(conflict("the session is running"));
            }
            match runs::latest_in(tx, &id)? {
                Some(r) if r.failed() => runs::reopen_in(tx, &live, &r),
                _ => Err(conflict("the latest run of this session did not fail; nothing to retry")),
            }
        })
        .await
}

/// 手动压缩（没在跑时）：记一条压缩的运行记录，失败也记在上面。
async fn compact(core: &Core, id: &str) -> Result<usize> {
    let (session_id, live) = (id.to_owned(), core.live.clone());
    let (run_id, agent_id) = core
        .db
        .write(move |tx| {
            let session = directory::get_in(tx, &session_id)?
                .ok_or_else(|| not_found(format!("session {session_id:?} not found")))?;
            // 正在删除的 agent 的会话不再新排工作（与来信同一条规则）
            muliao_assets::agents::active_agent_in(tx, &session.agent_id)?;
            Ok::<_, Error>((runs::start_in(tx, &live, &session_id, RunKind::Compaction)?, session.agent_id))
        })
        .await?;
    let result = async {
        let config = core.runtime.config(&agent_id).await?;
        core.compactor.compact(id, &config).await
    }
    .await;
    let error = result.as_ref().err().map(|e| e.to_string());
    let live = core.live.clone();
    let ended = core
        .db
        .write(move |tx| {
            let outcome = if error.is_some() { Outcome::Failed } else { Outcome::Done };
            runs::end_in(tx, &live, run_id, outcome, error.as_deref())
        })
        .await;
    if let Err(e) = ended {
        tracing::error!(session_id = id, error = %e, "recording a compaction failed");
    }
    match &result {
        Ok(0) => tracing::info!(session_id = id, "nothing to compact beyond the kept steps"),
        Ok(n) => tracing::info!(session_id = id, steps = n, "session compacted by the user"),
        Err(e) => tracing::error!(session_id = id, error = %e, "manual compaction failed"),
    }
    result
}
