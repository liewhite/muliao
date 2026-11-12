//! 推给前端的服务端事件（SSE `/api/events`）：只用于展示加速，库是唯一事实源，收到 changed 或重连时前端从接口重新拉取。
//!
//! 每条 data 是 `{"type": <事件名>, ...}`：总线上的事件只是「名字 + JSON」，这里补上 type。事件的形状由发布它的领域的
//! 具名类型定义，[`ServerEvent`] 把它们登记进 OpenAPI（前端类型由此生成），原样转发，前端忽略不认识的。

use std::convert::Infallible;
use std::time::Duration;

use axum::extract::State;
use axum::response::sse::{Event, Sse};
use futures::Stream;
use muliao_notifications::inbox::NotificationEvent;
use muliao_platform::bus::{ServerEvent as BusEvent, Subscription};
use muliao_sessions::live::{RunEventEnvelope, SavedEvent};
use serde::Serialize;
use serde_json::Value;
use utoipa::ToSchema;

use crate::state::AppState;

pub const HEARTBEAT: Duration = Duration::from_secs(15);

/// 服务端数据变了（不分类别）：正在显示的页面重新拉取。
#[derive(Serialize, ToSchema)]
pub struct ChangedEvent {}

/// 连上就发一次、之后每隔一段时间发一次：前端据此判断连接是否还活着（间隔随心跳下发，手机切后台、代理链路断开时
/// EventSource 往往收不到错误，只能靠「多久没收到消息」发现），并比较前端构建的版本（手机主屏应用常驻内存，升级后提示刷新）。
#[derive(Serialize, ToSchema)]
pub struct HeartbeatEvent {
    pub interval_seconds: f64,
    /// 服务端当前前端构建的版本；没有前端构建时为 null
    pub ui_version: Option<String>,
}

/// SSE 里每条 data 的形状（按 type 区分）。只用于接口描述：发出的是总线上各领域序列化好的事件加上 type。
#[derive(ToSchema)]
#[allow(dead_code)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ServerEvent {
    /// 运行中的实时事件（会话领域 live）
    Run(RunEventEnvelope),
    Saved(SavedEvent),
    Notification(NotificationEvent),
    Changed(ChangedEvent),
    Heartbeat(HeartbeatEvent),
}

/// 一条 SSE：data 是事件的字段加上 type。不是对象的是缺陷，记 error 不发出。
fn tagged(kind: &str, payload: Value) -> Option<Event> {
    let Value::Object(mut fields) = payload else {
        tracing::error!(kind, "server event data is not an object, dropped");
        return None;
    };
    fields.insert("type".into(), Value::from(kind));
    Some(Event::default().data(Value::Object(fields).to_string()))
}

fn from_bus(BusEvent { kind, data }: BusEvent) -> Option<Event> {
    tagged(kind, data)
}

fn heartbeat(ui_version: &Option<String>) -> Option<Event> {
    let beat = HeartbeatEvent { interval_seconds: HEARTBEAT.as_secs_f64(), ui_version: ui_version.clone() };
    tagged("heartbeat", serde_json::to_value(beat).expect("heartbeats serialize"))
}

enum Step {
    Start(Subscription),
    Next(Subscription),
}

/// 事件流：连上先发一次心跳（订阅已建立，前端此时重新拉取不会漏事件），之后有事件就发，空闲满一个间隔发心跳；服务关停
/// （总线关闭）或跟不上被断开时结束，浏览器自己重连。断线重连的间隔用浏览器的默认值。
#[utoipa::path(get, path = "/api/events", tag = "events",
    responses((status = 200, description = "事件流（text/event-stream），每条 data 是一个 ServerEvent", content_type = "text/event-stream", body = ServerEvent)))]
pub async fn events(State(state): State<AppState>) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let subscription = state.services.bus.subscribe();
    let ui_version = state.ui_version.clone();
    let stream = futures::stream::unfold(Step::Start(subscription), move |step| {
        let ui_version = ui_version.clone();
        async move {
            match step {
                Step::Start(sub) => Some((heartbeat(&ui_version), Step::Next(sub))),
                Step::Next(mut sub) => match tokio::time::timeout(HEARTBEAT, sub.next()).await {
                    Err(_) => Some((heartbeat(&ui_version), Step::Next(sub))),
                    Ok(Ok(event)) => Some((from_bus(event), Step::Next(sub))),
                    Ok(Err(_)) => None,
                },
            }
        }
    });
    let stream = futures::StreamExt::filter_map(stream, |e| async move { e.map(Ok) });
    Sse::new(stream)
}
