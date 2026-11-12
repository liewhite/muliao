//! 推给前端 SSE 的展示事件（server event）。只用于展示加速：不持久化、不保证送达，数据库是唯一事实源。订阅者跟不上
//! 时直接结束它的订阅，前端重连后从数据库重新拉取。服务关停时关闭总线，结束所有订阅，SSE 连接随之正常结束（#330）。
//!
//! 与领域事件（在发布方事务里同步调用订阅者）不是一回事：这里的事件发出就不管了。

use std::sync::Mutex;

use serde::Serialize;
use serde_json::Value;
use tokio::sync::broadcast;

/// 一个展示事件：`kind` 是 SSE 的事件名，`data` 是发给前端的 JSON。平台只定义 [`ServerEvent::changed`]，其余由发布方
/// 定义（如会话运行中的流式片段）。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ServerEvent {
    pub kind: &'static str,
    pub data: Value,
}

impl ServerEvent {
    /// 服务端数据变了（#646 起不分类别）：正在显示的页面重新拉取。写过表的事务提交后发（[`crate::db`]），资源文件有改动
    /// 时也发。不管是谁改的都一样。
    pub fn changed() -> Self {
        Self { kind: "changed", data: Value::Object(Default::default()) }
    }
}

/// 订阅结束的原因
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum Ended {
    #[error("event subscriber fell behind and was disconnected")]
    FellBehind,
    #[error("event bus closed: the service is shutting down")]
    Closed,
}

pub struct Bus {
    sender: Mutex<Option<broadcast::Sender<ServerEvent>>>,
}

impl Bus {
    /// buffer：每个订阅者最多积压多少个事件，再多就结束它的订阅
    pub fn new(buffer: usize) -> Self {
        Self { sender: Mutex::new(Some(broadcast::channel(buffer).0)) }
    }

    pub fn publish(&self, event: ServerEvent) {
        if let Some(sender) = self.sender.lock().expect("bus lock").as_ref() {
            // 没有订阅者时 send 报错，这是正常的
            let _ = sender.send(event);
        }
    }

    pub fn changed(&self) {
        self.publish(ServerEvent::changed());
    }

    pub fn subscribe(&self) -> Subscription {
        match self.sender.lock().expect("bus lock").as_ref() {
            Some(sender) => Subscription { state: Ok(sender.subscribe()) },
            None => Subscription { state: Err(Ended::Closed) },
        }
    }

    /// 服务关停：结束现有的订阅（积压的事件照常取完），之后的订阅一开始就结束，之后发布的事件没有人收。
    pub fn close(&self) {
        self.sender.lock().expect("bus lock").take();
    }
}

pub struct Subscription {
    state: Result<broadcast::Receiver<ServerEvent>, Ended>,
}

impl Subscription {
    /// 取下一个事件；可以安全地被超时取消（只取消这一次读取）。订阅结束后一直返回 Err。
    pub async fn next(&mut self) -> Result<ServerEvent, Ended> {
        let receiver = match &mut self.state {
            Ok(receiver) => receiver,
            Err(ended) => return Err(ended.clone()),
        };
        let ended = match receiver.recv().await {
            Ok(event) => return Ok(event),
            Err(broadcast::error::RecvError::Closed) => Ended::Closed,
            Err(broadcast::error::RecvError::Lagged(_)) => {
                tracing::warn!("event subscriber overflowed, disconnecting it");
                Ended::FellBehind
            }
        };
        self.state = Err(ended.clone());
        Err(ended)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn delivers_then_ends_on_close_or_lag() {
        let bus = Bus::new(2);
        let mut a = bus.subscribe();
        bus.changed();
        assert_eq!(a.next().await.unwrap(), ServerEvent::changed());

        for _ in 0..3 {
            bus.changed();
        }
        assert_eq!(a.next().await, Err(Ended::FellBehind));
        assert_eq!(a.next().await, Err(Ended::FellBehind));

        let mut b = bus.subscribe();
        bus.changed();
        bus.close();
        // 积压的照常取完，之后结束
        assert!(b.next().await.is_ok());
        assert_eq!(b.next().await, Err(Ended::Closed));
        assert_eq!(bus.subscribe().next().await, Err(Ended::Closed));
    }
}
