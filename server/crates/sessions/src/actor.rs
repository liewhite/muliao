//! 会话 actor：每个会话一个协程，信箱里有要处理的信就跑一次运行，一次一个。没有任务队列（#675）：要不要跑只看库里的信、
//! 历史与运行记录（[`should_run`]，唯一规则），所以投信只需在提交后叫醒它（[`Waker`]），重启后扫一遍就能接着跑。

use muliao_platform::db::Tx;
use tokio::sync::mpsc;

use crate::mailbox::Mail;
use crate::pending::Position;
use crate::runs::RunRecord;

/// 叫醒会话 actor 的句柄：只说「这个会话可能有事了」，要不要跑由 actor 自己按 [`should_run`] 判断。可以先于 actor 系统
/// 构造（组合根里没有构造环）。
#[derive(Clone)]
pub struct Waker {
    tx: mpsc::UnboundedSender<String>,
}

impl Waker {
    /// 一个叫醒句柄与它的接收端（交给 actor 系统）。
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
