//! 会话领域对外发布的事件：发布方不该知道谁在听（通知、issue、工作区各自决定怎么用）。在发布方的事务里按登记顺序同步调用
//! 每个订阅者，订阅者的写入与发布方一起提交或回滚，出错交给发布方（docs/architecture.md 3.3）。订阅者只做短的数据库
//! 操作；事务外的事用 `Tx::on_commit` 排到提交之后。
//!
//! 订阅在组合根装配时登记一次（[`Events::subscribe`]），之后不变；没登记完就发布是装配漏了，当场 panic。

use std::sync::{Arc, OnceLock};

use muliao_platform::db::Tx;

use crate::answers::Waiting;
use crate::origins::SessionOrigin;

/// 一次运行结束了（完成、停在挂起的调用上、被停止）；没有可交给模型的空闲收尾不发。只带事实。
#[derive(Debug, Clone, PartialEq)]
pub struct RunEnded {
    pub session_id: String,
    pub session_title: String,
    pub origin: SessionOrigin,
    pub agent_id: String,
    pub agent_name: String,
    pub run_id: i64,
    /// 模型的最后回答；停在提问、等批准上时是问题与等批准的调用，只停在等 agent 上时为空
    pub answer: String,
    pub stopped: bool,
    /// 停在哪几类挂起的调用上（不重复）；空是没有停在挂起的调用上
    pub waiting_on: Vec<Waiting>,
    /// 这次运行处理过的信件里有用户在等的
    pub awaited: bool,
}

/// 一次运行最终失败了（暂时性失败的重试用完，或契约外的失败）。不自动重试。
#[derive(Debug, Clone, PartialEq)]
pub struct RunFailed {
    pub session_id: String,
    pub agent_id: String,
    pub agent_name: String,
    pub run_id: i64,
    pub origin: SessionOrigin,
    /// 失败的原文，怎么截给人看由订阅方定
    pub error: String,
}

/// 会话被停止：cleared 是被清掉的外部工作的占用者（如 job:12，它在外面的工作被放弃；委派由会话领域自己停）。
#[derive(Debug, Clone, PartialEq)]
pub struct SessionStopped {
    pub session_id: String,
    pub cleared: Vec<String>,
}

/// 会话被删除：引用它的数据由各领域清理。
#[derive(Debug, Clone, PartialEq)]
pub struct SessionDeleted {
    pub session_id: String,
    pub agent_id: String,
}

pub type BoxError = Box<dyn std::error::Error + Send + Sync>;
type Handler<E> = Arc<dyn Fn(&mut Tx<'_>, &E) -> Result<(), BoxError> + Send + Sync>;

/// 各领域登记的订阅者。
#[derive(Default, Clone)]
pub struct Subscribers {
    pub run_ended: Vec<Handler<RunEnded>>,
    pub run_failed: Vec<Handler<RunFailed>>,
    pub session_stopped: Vec<Handler<SessionStopped>>,
    pub session_deleted: Vec<Handler<SessionDeleted>>,
}

impl Subscribers {
    pub fn on_run_ended(&mut self, f: impl Fn(&mut Tx<'_>, &RunEnded) -> Result<(), BoxError> + Send + Sync + 'static) {
        self.run_ended.push(Arc::new(f));
    }
    pub fn on_run_failed(&mut self, f: impl Fn(&mut Tx<'_>, &RunFailed) -> Result<(), BoxError> + Send + Sync + 'static) {
        self.run_failed.push(Arc::new(f));
    }
    pub fn on_session_stopped(
        &mut self,
        f: impl Fn(&mut Tx<'_>, &SessionStopped) -> Result<(), BoxError> + Send + Sync + 'static,
    ) {
        self.session_stopped.push(Arc::new(f));
    }
    pub fn on_session_deleted(
        &mut self,
        f: impl Fn(&mut Tx<'_>, &SessionDeleted) -> Result<(), BoxError> + Send + Sync + 'static,
    ) {
        self.session_deleted.push(Arc::new(f));
    }
}

/// 发布点：组合根登记一次订阅者，之后会话领域在自己的事务里发布。
#[derive(Default)]
pub struct Events {
    subscribers: OnceLock<Subscribers>,
}

impl Events {
    /// 登记全部订阅者（只能一次）。
    pub fn subscribe(&self, subscribers: Subscribers) {
        assert!(self.subscribers.set(subscribers).is_ok(), "session event subscribers are registered once");
    }

    fn all(&self) -> &Subscribers {
        self.subscribers.get().expect("session events published before their subscribers were registered")
    }

    pub(crate) fn run_ended(&self, tx: &mut Tx<'_>, e: &RunEnded) -> crate::error::Result<()> {
        publish(tx, &self.all().run_ended, e)
    }
    pub(crate) fn run_failed(&self, tx: &mut Tx<'_>, e: &RunFailed) -> crate::error::Result<()> {
        publish(tx, &self.all().run_failed, e)
    }
    pub(crate) fn session_stopped(&self, tx: &mut Tx<'_>, e: &SessionStopped) -> crate::error::Result<()> {
        publish(tx, &self.all().session_stopped, e)
    }
    pub(crate) fn session_deleted(&self, tx: &mut Tx<'_>, e: &SessionDeleted) -> crate::error::Result<()> {
        publish(tx, &self.all().session_deleted, e)
    }
}

fn publish<E>(tx: &mut Tx<'_>, handlers: &[Handler<E>], event: &E) -> crate::error::Result<()> {
    for h in handlers {
        h(tx, event).map_err(crate::error::Error::Subscriber)?;
    }
    Ok(())
}
