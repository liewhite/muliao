//! 按到期时间触发的方式共用的激活机制：扫描到期的触发器，到点各触发一次。
//!
//! 定时与一次性的差别只有两件事：到期时间从哪来（[`crate::kinds::Mode::due`]）、这次到期被用掉之后这一行怎么变
//! （[`consume_in`]）；扫描、并发触发与下次唤醒在这里实现一次。
//!
//! 用最终正确性换简化：消耗这次到期与触发不在一个事务里（触发可能先跑前置检查，不能占着事务）。进程恰好死在两者之间时
//! 这一次被跳过（至多一次），换来的是检查失败或很慢都不会让同一个时刻反复触发。

use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_notifications::Notifications;
use muliao_platform::db::Tx;
use muliao_platform::ids::TriggerId;
use tokio::task::JoinHandle;

use crate::kinds::Due;
use crate::schedule;
use crate::service::TriggerService;
use crate::store::{Listing, Trigger, load_in, save_in};
use muliao_platform::errors::{Error, Result};

/// 没有更早到期的也至少这么久醒一次
const MAX_SLEEP: Duration = Duration::from_secs(60);
/// 两轮至少隔这么久：消耗写不进去（磁盘满）时它仍到期，不能零间隔空转刷日志
const MIN_SLEEP: Duration = Duration::from_secs(1);

pub struct DueRunner {
    service: TriggerService,
    tz: Tz,
    notifications: Notifications,
    /// 触发还没做完的（前置检查在跑时还没有会话，定时的「上一次会话不空闲就跳过」看不到它）：它再到期时只消耗不触发，同一个
    /// 触发器不重叠。只在内存：服务重启时在跑的触发随进程结束
    firing: Arc<Mutex<HashSet<TriggerId>>>,
}

/// 从 firing 里移走（触发做完、出错或被取消都会走到）
struct Firing(Arc<Mutex<HashSet<TriggerId>>>, TriggerId);

impl Drop for Firing {
    fn drop(&mut self) {
        self.0.lock().expect("firing lock").remove(&self.1);
    }
}

impl DueRunner {
    pub fn new(service: TriggerService, tz: Tz, notifications: Notifications) -> Self {
        Self { service, tz, notifications, firing: Arc::default() }
    }

    /// 常驻：扫描一轮、睡到最早的到期时间（最多一分钟），有触发器改动就提前醒。
    pub fn run(self: Arc<Self>) -> JoinHandle<()> {
        tokio::spawn(async move {
            let mut changed = self.service.watch();
            loop {
                changed.borrow_and_update();
                self.fire_due(Utc::now()).await;
                let wait = self.next_wake().await;
                tokio::select! {
                    _ = changed.changed() => {}
                    _ = tokio::time::sleep(wait) => {}
                }
            }
        })
    }

    /// 还会到期的触发器与它们的到期时间。列不出来（数据库出错）记 error，这一轮当作没有。
    async fn pending(&self) -> Vec<(TriggerId, DateTime<Utc>)> {
        let listing = Listing { enabled_only: true, ..Listing::default() };
        match self.service.list(listing).await {
            Ok(triggers) => triggers.into_iter().filter_map(|mut t| Some((t.id, t.mode.due()?.at()))).collect(),
            Err(e) => {
                tracing::error!(error = %e, "listing due triggers failed");
                Vec::new()
            }
        }
    }

    /// 扫描一轮：到期的在这里逐个消耗（短事务，下一轮不会再看到它到期），触发放出去并发跑、不等：一个慢的前置检查既不拖住
    /// 同一轮的其他触发器，也不推迟下一轮扫描。返回放出去的触发（测试等它们做完）。
    pub async fn fire_due(&self, now: DateTime<Utc>) -> Vec<JoinHandle<()>> {
        let mut fired = Vec::new();
        for (trigger_id, at) in self.pending().await {
            if at > now || !self.consume(trigger_id, now).await {
                continue;
            }
            if !self.firing.lock().expect("firing lock").insert(trigger_id) {
                tracing::info!(trigger_id = trigger_id.get(), "due fire skipped: its previous fire is still running");
                continue;
            }
            let guard = Firing(self.firing.clone(), trigger_id);
            let service = self.service.clone();
            fired.push(tokio::spawn(async move {
                let _guard = guard;
                // 契约之外的失败只记在它自己身上，不再重试（至多一次）
                if let Err(e) = service.fire_activated(trigger_id, None).await {
                    tracing::error!(trigger_id = trigger_id.get(), error = %e, "due fire failed");
                }
            }));
        }
        fired
    }

    /// 按最新的行消耗这次到期，返回是否应当触发。失败（数据库出错）只记日志，没提交的下一轮重来。
    async fn consume(&self, trigger_id: TriggerId, now: DateTime<Utc>) -> bool {
        let (tz, notifications) = (self.tz, self.notifications.clone());
        let consumed = self
            .service
            .db()
            .write(move |tx| {
                // 列出之后可能已被删除、停用、结束或改了配置：按最新的行重新判断
                let Some(mut t) = load_in(tx, trigger_id)? else { return Ok::<_, Error>(Some("deleted".to_owned())) };
                if t.finished_at.is_some() {
                    return Ok(Some("finished".into()));
                }
                if !t.enabled {
                    return Ok(Some("disabled".into()));
                }
                let skipped = consume_in(tx, &mut t, now, tz, &notifications)?;
                save_in(tx, &t)?;
                Ok(skipped)
            })
            .await;
        match consumed {
            Ok(None) => true,
            Ok(Some(why)) => {
                tracing::info!(trigger_id = trigger_id.get(), why, "due fire skipped");
                false
            }
            Err(e) => {
                tracing::error!(trigger_id = trigger_id.get(), error = %e, "due consume failed");
                false
            }
        }
    }

    async fn next_wake(&self) -> Duration {
        let now = Utc::now();
        self.pending()
            .await
            .into_iter()
            .map(|(_, at)| (at - now).to_std().unwrap_or(Duration::ZERO))
            .min()
            .map_or(MAX_SLEEP, |d| d.clamp(MIN_SLEEP, MAX_SLEEP))
    }
}

/// 在到期的事务里消耗这次到期：改这一行（调用方写回），使它不再在 now 到期——定时推进到下次，一次性的结束。返回
/// Some(原因) 表示这次不触发（记日志）；列出之后改了配置、还没到期的不动。
fn consume_in(
    tx: &mut Tx<'_>,
    t: &mut Trigger,
    now: DateTime<Utc>,
    tz: Tz,
    notifications: &Notifications,
) -> Result<Option<String>> {
    let mut mode = t.mode.clone();
    let Some(due) = mode.due().filter(|d| d.at() <= now) else { return Ok(Some("rescheduled".into())) };
    let skipped = match due {
        Due::Schedule { config, state } => schedule::consume_in(tx, t, config, state, now, tz, notifications)?,
        // 到点让它结束，这一次就算用掉了
        Due::Once { .. } => {
            t.finished_at = Some(now);
            tracing::info!(trigger = %t.name, "once trigger finished");
            None
        }
    };
    t.mode = mode;
    Ok(skipped)
}
