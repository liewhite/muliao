//! 运行中的实时展示：当前这一段的运行事件在内存里留一份，经展示总线推给前端 SSE。只用于展示加速，库是唯一事实源。
//!
//! 一段从一次模型请求保存之后开始，包括模型的回复与它调用的工具，到下一次请求保存（或运行结束）为止。每保存一次请求就换一段
//! （[`Live::step`]），发 `saved` 让前端重新拉取历史；回复在工具执行前就已保存，但这一段的事件已经在展示它，会话接口不在
//! 历史里重复显示（after_message_id）。事件带这一段的 segment_id 与递增的 seq，前端据此把快照与之后的 SSE 事件合并、发现缺口。
//! 生命周期只由运行管：开始时拿一个守卫（[`Live::begin`]），丢掉它就结束；[`Live::step`] 换段。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use chrono::{DateTime, Utc};
use muliao_platform::bus::{Bus, ServerEvent};
use muliao_platform::db::Tx;
use serde::Serialize;
use utoipa::ToSchema;

use crate::transcript::Item;

const PREVIEW_CHARS: usize = 2000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum DeltaType {
    Assistant,
    Thinking,
}

/// 一条运行事件。
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum RunEvent {
    /// 一条完整的会话条目（工具调用、工具结果），与历史同一个投影；文字可能截断过
    Item { item: Box<Item> },
    /// 流式文字：接在同类的最后一条后面（或开始新的一条）
    Delta { item_type: DeltaType, text: String },
}

/// 推给前端的一条（SSE 事件名 `run`）。
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct RunEventEnvelope {
    pub session_id: String,
    pub agent: String,
    pub segment_id: String,
    pub seq: usize,
    #[serde(flatten)]
    pub event: RunEvent,
}

#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct LiveSegment {
    pub segment_id: String,
    /// 这一段开始时会话最后一条已保存消息的 id：这之后保存的消息由这一段的事件展示，历史里先不显示
    pub after_message_id: Option<i64>,
    pub events: Vec<RunEventEnvelope>,
}

/// 这个会话的历史或运行状态变了（SSE 事件名 `saved`）：运行中又保存了一步（实时事件换成新的一段），或运行记录变了
/// （运行或压缩开始、结束、失败、停止、重试）。会话页只按它重新拉取：整份会话可能有几 MB，不随全局的 changed 拉。
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct SavedEvent {
    pub session_id: String,
}

/// 运行出了暂时性的错、在等下一次尝试（只在内存里，重启后从上一步续跑、不再有它）：会话页显示错误与下次尝试的时间。
#[derive(Debug, Clone, PartialEq, Serialize, ToSchema)]
pub struct Retrying {
    pub error: String,
    pub next_at: DateTime<Utc>,
}

/// 一个会话在内存里的运行状态：正在展示的这一段、在等的重试。两样都没有时不留记录。
#[derive(Default)]
struct State {
    segment: Option<LiveSegment>,
    retrying: Option<Retrying>,
}

#[derive(Clone)]
pub struct Live {
    bus: Arc<Bus>,
    states: Arc<Mutex<HashMap<String, State>>>,
}

fn publish_saved(bus: &Bus, session_id: &str) {
    let saved = SavedEvent { session_id: session_id.to_owned() };
    bus.publish(ServerEvent { kind: "saved", data: serde_json::to_value(saved).expect("saved events serialize") });
}

fn new_segment(after: Option<i64>) -> LiveSegment {
    LiveSegment { segment_id: uuid::Uuid::new_v4().simple().to_string(), after_message_id: after, events: Vec::new() }
}

/// 一次运行的实时展示：拿着它期间这个会话有正在展示的一段，丢掉（运行返回、被停止时丢掉、panic）就结束。
pub(crate) struct LiveRun {
    live: Live,
    session_id: String,
}

impl Drop for LiveRun {
    fn drop(&mut self) {
        self.live.update(&self.session_id, |s| s.segment = None);
    }
}

/// 一次运行连同它的重试：拿着它期间显示的重试状态，丢掉（结束了：成功、最终失败或停止）就清掉。两次尝试之间不清：下一次
/// 尝试进行中仍显示上次的错误。
pub(crate) struct Attempts {
    live: Live,
    session_id: String,
}

impl Attempts {
    /// 这次尝试暂时性地失败了，`delay` 之后再试。
    pub(crate) fn retrying(&self, error: &str, delay: std::time::Duration) {
        let next_at = Utc::now() + chrono::Duration::from_std(delay).expect("retry delays are minutes");
        let state = Retrying { error: error.to_owned(), next_at };
        self.live.update(&self.session_id, |s| s.retrying = Some(state));
        publish_saved(&self.live.bus, &self.session_id);
    }
}

impl Drop for Attempts {
    fn drop(&mut self) {
        if self.live.update(&self.session_id, |s| s.retrying.take()).is_some() {
            publish_saved(&self.live.bus, &self.session_id);
        }
    }
}

impl Live {
    pub fn new(bus: Arc<Bus>) -> Self {
        Self { bus, states: Arc::default() }
    }

    /// 改一个会话的状态；改完两样都没有就去掉记录。
    fn update<R>(&self, session_id: &str, f: impl FnOnce(&mut State) -> R) -> R {
        let mut all = self.states.lock().expect("live lock");
        let state = all.entry(session_id.to_owned()).or_default();
        let r = f(state);
        if state.segment.is_none() && state.retrying.is_none() {
            all.remove(session_id);
        }
        r
    }

    /// 一次运行（连同重试）开始。
    pub(crate) fn attempts(&self, session_id: &str) -> Attempts {
        Attempts { live: self.clone(), session_id: session_id.to_owned() }
    }

    pub(crate) fn retry_state(&self, session_id: &str) -> Option<Retrying> {
        self.states.lock().expect("live lock").get(session_id).and_then(|s| s.retrying.clone())
    }

    /// 一次运行开始实时展示。同一会话同时只有一次运行（actor 保证）。
    pub(crate) fn begin(&self, session_id: &str, after_message_id: Option<i64>) -> LiveRun {
        self.update(session_id, |s| s.segment = Some(new_segment(after_message_id)));
        LiveRun { live: self.clone(), session_id: session_id.to_owned() }
    }

    /// 一次请求已经保存（事务已提交）：之后的事件属于新的一段。
    pub(crate) fn step(&self, session_id: &str, after_message_id: i64) {
        self.update(session_id, |s| s.segment = Some(new_segment(Some(after_message_id))));
        publish_saved(&self.bus, session_id);
    }

    /// 这个事务改了会话的运行记录：提交后发 `saved`。
    pub(crate) fn saved_on_commit(&self, tx: &mut Tx<'_>, session_id: &str) {
        let (bus, session_id) = (self.bus.clone(), session_id.to_owned());
        tx.on_commit(move || publish_saved(&bus, &session_id));
    }

    pub(crate) fn item(&self, session_id: &str, agent: &str, item: Item) {
        self.publish(session_id, agent, RunEvent::Item { item: Box::new(item.preview(PREVIEW_CHARS)) });
    }

    pub(crate) fn delta(&self, session_id: &str, agent: &str, item_type: DeltaType, text: &str) {
        if !text.is_empty() {
            self.publish(session_id, agent, RunEvent::Delta { item_type, text: text.to_owned() });
        }
    }

    fn publish(&self, session_id: &str, agent: &str, event: RunEvent) {
        let envelope = {
            let mut all = self.states.lock().expect("live lock");
            // 运行结束后才到的事件（不会有：事件都在运行里发出）不展示
            let Some(segment) = all.get_mut(session_id).and_then(|s| s.segment.as_mut()) else { return };
            let envelope = RunEventEnvelope {
                session_id: session_id.to_owned(),
                agent: agent.to_owned(),
                segment_id: segment.segment_id.clone(),
                seq: segment.events.len(),
                event,
            };
            segment.events.push(envelope.clone());
            envelope
        };
        self.bus
            .publish(ServerEvent { kind: "run", data: serde_json::to_value(envelope).expect("run events serialize") });
    }

    pub(crate) fn snapshot(&self, session_id: &str) -> Option<LiveSegment> {
        self.states.lock().expect("live lock").get(session_id).and_then(|s| s.segment.clone())
    }
}
