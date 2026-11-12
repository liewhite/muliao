//! 会话：列表、新建、会话页、发消息（含回答提问与批准调用）、停止、手动压缩、重试最终失败的运行、标记已读、删除。

use std::collections::{BTreeMap, BTreeSet};

use axum::extract::State;
use axum::http::StatusCode;
use chrono::{DateTime, Utc};
use muliao_sessions::activity::{Wait, WorkState};
use muliao_sessions::answers::{CallResult, Choice, QuestionAnswer};
use muliao_sessions::directory::{Listing, Session};
use muliao_sessions::inbound::{UserMessage, UserTarget};
use muliao_sessions::live::LiveSegment;
use muliao_sessions::mailbox::{Mail, MailKind, MailState};
use muliao_sessions::origins::{Origin, Sender, SessionOrigin};
use muliao_sessions::runs::RunRecord;
use muliao_sessions::transcript::Item;
use muliao_sessions::work::EventSource;
use muliao_usage::UsageTotals;
use serde::{Deserialize, Serialize};
use utoipa::{IntoParams, ToSchema};
use utoipa_axum::router::OpenApiRouter;
use utoipa_axum::routes;

use crate::api::files::FileOut;
use crate::error::{ApiError, ApiResult, ErrorOut};
use crate::extract::{Json, Path, Query};
use crate::state::AppState;

pub fn routes() -> OpenApiRouter<AppState> {
    OpenApiRouter::new()
        .routes(routes!(list_sessions, start_session))
        .routes(routes!(get_session, delete_session))
        .routes(routes!(reply))
        .routes(routes!(stop))
        .routes(routes!(compact))
        .routes(routes!(retry))
        .routes(routes!(mark_read))
}

#[derive(Serialize, ToSchema)]
pub struct SessionOut {
    pub id: String,
    pub agent_id: String,
    pub title: String,
    /// 会话的来源与发起方（结构化，前端不认识库里的编号格式）
    pub origin: SessionOrigin,
    /// 委派产生的子会话：父会话与 thread 名
    pub parent_session_id: Option<String>,
    pub thread: Option<String>,
    /// 有指向它的未读通知（用户在等的回答、提问、agent 主动通知、运行失败）：未读不另存，从通知派生
    pub unread: bool,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl SessionOut {
    fn of(s: Session, unread: &BTreeSet<String>) -> Self {
        SessionOut {
            unread: unread.contains(&s.id),
            id: s.id,
            agent_id: s.agent_id,
            title: s.title,
            origin: s.origin,
            parent_session_id: s.parent_session_id,
            thread: s.thread,
            created_at: s.created_at,
            updated_at: s.updated_at,
        }
    }
}

/// SessionOut 的唯一构造入口：一次取齐这些会话的未读（通知领域）。
async fn session_outs(s: &AppState, sessions: Vec<Session>) -> ApiResult<Vec<SessionOut>> {
    let ids = sessions.iter().map(|x| x.id.clone()).collect();
    let unread = s.services.notifications.unread_sessions(ids).await?;
    Ok(sessions.into_iter().map(|x| SessionOut::of(x, &unread)).collect())
}

async fn session_out(s: &AppState, session: Session) -> ApiResult<SessionOut> {
    Ok(session_outs(s, vec![session]).await?.pop().expect("one in, one out"))
}

/// 会话列表里的一行：多一个只在列表里算的工作状态（会话页看 SessionDetail.state）。
#[derive(Serialize, ToSchema)]
pub struct SessionListItem {
    #[serde(flatten)]
    pub session: SessionOut,
    pub state: WorkState,
}

#[derive(Deserialize, IntoParams)]
#[into_params(parameter_in = Query)]
pub struct ListQuery {
    pub agent_id: Option<String>,
    /// 只列这些来源的会话（可以重复给）
    #[serde(default)]
    #[param(value_type = Option<Vec<String>>)]
    pub origin: Option<Vec<Origin>>,
    /// 只列这个触发器发起的会话（不能再给 origin）
    pub trigger_id: Option<i64>,
    /// 关键字：只列标题或消息含有它的会话（不区分 ASCII 大小写；不能为空串）
    pub q: Option<String>,
    /// 1–200，默认 50
    pub limit: Option<usize>,
    pub offset: Option<usize>,
}

/// 工作状态不是 idle 的会话（在全部符合筛选的会话里，不只是这一页）置顶，其余按最近更新排序。
#[utoipa::path(get, path = "/api/sessions", tag = "sessions", params(ListQuery),
    responses((status = 200, body = Vec<SessionListItem>), (status = 422, body = ErrorOut)))]
async fn list_sessions(
    State(s): State<AppState>,
    Query(q): Query<ListQuery>,
) -> ApiResult<axum::Json<Vec<SessionListItem>>> {
    let sender = match (q.trigger_id, &q.origin) {
        (Some(_), Some(_)) => return Err(ApiError::invalid("give either trigger_id or origin, not both")),
        (Some(trigger_id), None) => Some(Sender::Trigger { trigger_id }),
        (None, _) => None,
    };
    if q.q.as_deref() == Some("") {
        return Err(ApiError::invalid("q must not be empty"));
    }
    let limit = q.limit.unwrap_or(50);
    if !(1..=200).contains(&limit) {
        return Err(ApiError::invalid("limit must be between 1 and 200"));
    }
    let listing = Listing { agent_id: q.agent_id, origins: q.origin, sender, keyword: q.q };
    let listed = s.services.sessions.list(listing, limit, q.offset.unwrap_or(0)).await?;
    let states: Vec<WorkState> = listed.iter().map(|x| x.state).collect();
    let outs = session_outs(&s, listed.into_iter().map(|x| x.session).collect()).await?;
    Ok(axum::Json(outs.into_iter().zip(states).map(|(session, state)| SessionListItem { session, state }).collect()))
}

#[derive(Deserialize, ToSchema)]
pub struct StartIn {
    pub agent_id: String,
    pub prompt: String,
    /// 先经 POST /api/files 上传得到的 id
    #[serde(default)]
    pub attachments: Vec<String>,
}

/// 新建会话并发出第一条消息。
#[utoipa::path(post, path = "/api/sessions", tag = "sessions", request_body = StartIn,
    responses((status = 201, body = SessionOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)))]
async fn start_session(
    State(s): State<AppState>,
    Json(body): Json<StartIn>,
) -> ApiResult<(StatusCode, axum::Json<SessionOut>)> {
    let session = s
        .services
        .sessions
        .receive(UserMessage {
            content: body.prompt,
            session: UserTarget::NewChat { agent_id: body.agent_id },
            attachments: body.attachments,
            answers: BTreeMap::new(),
            approvals: BTreeMap::new(),
        })
        .await?;
    Ok((StatusCode::CREATED, axum::Json(session_out(&s, session).await?)))
}

/// 会话信箱里的一封信件（显示在历史之后：排队中的，以及最近一次停止丢弃的）。
#[derive(Serialize, ToSchema)]
pub struct MailOut {
    pub id: i64,
    pub content: String,
    pub attachments: Vec<FileOut>,
    pub state: MailState,
    pub created_at: DateTime<Utc>,
    #[serde(flatten)]
    pub kind: MailKindOut,
}

#[derive(Serialize, ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum MailKindOut {
    Message,
    Event {
        /// 事件的来源
        source: EventSource,
        /// 事件是运行中检查（工作还在进行）
        check_in: bool,
    },
    Answer {
        /// tool_call_id -> 那次挂起调用的结果（用户的回答，或被问 agent 的结局）
        answers: BTreeMap<String, CallResult>,
    },
}

/// MailOut 的唯一构造入口：一次取齐这些信件的附件。
async fn mail_outs(s: &AppState, mail: Vec<Mail>) -> ApiResult<Vec<MailOut>> {
    let mut ids: Vec<String> = mail.iter().flat_map(|m| m.attachments.iter().cloned()).collect();
    ids.sort();
    ids.dedup();
    let files: BTreeMap<String, FileOut> =
        s.services.files.referenced(ids).await?.iter().map(|f| (f.id.clone(), FileOut::of(f))).collect();
    mail.into_iter()
        .map(|m| {
            let kind = match m.kind {
                MailKind::Message => MailKindOut::Message,
                MailKind::Event => MailKindOut::Event { source: m.event_source()?, check_in: m.check_in },
                MailKind::Answer => MailKindOut::Answer { answers: m.answers },
            };
            Ok(MailOut {
                attachments: m.attachments.iter().map(|id| files[id].clone()).collect(),
                id: m.id,
                content: m.content,
                state: m.state,
                created_at: m.created_at,
                kind,
            })
        })
        .collect()
}

#[derive(Serialize, ToSchema)]
pub struct CompactionOut {
    /// 这条消息及之前的已由 summary 代替发给模型（原文仍完整展示）
    pub through_message_id: i64,
    pub summary: String,
    pub created_at: DateTime<Utc>,
}

/// 在等的后台任务的详情（与 job_status 同一份）。
#[derive(Serialize, ToSchema)]
pub struct JobOut {
    pub command: String,
    /// running、exited、killed、interrupted：取等待项与取任务信息不在同一事务，之间任务可能已经结束（结局还没交回）
    pub status: String,
    /// 日志，相对 agent 工作目录
    pub log_path: String,
    /// 日志最后几行
    pub tail: String,
}

/// 会话在等的一项东西；后台任务（kind 为 job）带上任务的详情。
#[derive(Serialize, ToSchema)]
pub struct WaitOut {
    #[serde(flatten)]
    pub wait: Wait,
    /// 后台任务的详情；别的种类为 null
    pub job: Option<JobOut>,
}

/// WaitOut 的唯一构造入口：后台任务向工作区领域取命令与日志。
async fn wait_outs(s: &AppState, agent_id: &str, waits: Vec<Wait>) -> ApiResult<Vec<WaitOut>> {
    let mut out = Vec::with_capacity(waits.len());
    for wait in waits {
        let job = match &wait {
            Wait::Job { job_id, .. } => {
                let info = s.services.jobs.info(*job_id, agent_id).await?;
                Some(JobOut {
                    command: info.command,
                    status: info.status.to_owned(),
                    log_path: info.log_path,
                    tail: info.tail,
                })
            }
            _ => None,
        };
        out.push(WaitOut { wait, job });
    }
    Ok(out)
}

/// 发给模型的上下文有多大、到多大会压缩。
#[derive(Serialize, ToSchema)]
pub struct ContextOut {
    /// 现在的大小：上一次模型请求的输入 token（含缓存读写）；还没有模型回复、或刚压缩过还没有新回复时为 null；0 表示模型
    /// 没有报告用量
    pub tokens: Option<u64>,
    /// 上下文超过它就压缩
    pub threshold_tokens: u64,
}

#[derive(Serialize, ToSchema)]
pub struct SessionDetail {
    pub session: SessionOut,
    /// 完整原始历史的展示条目（正在进行的那一段由 live 展示）
    pub items: Vec<Item>,
    /// 压缩点：through_message_id 及之前的消息已由 summary 代替发给模型（原文仍完整展示）
    pub compactions: Vec<CompactionOut>,
    /// 显示在历史之后的信件：排队中的（处理中的用户消息、已提交的回答），以及最近一次停止丢弃的
    pub mail: Vec<MailOut>,
    /// 最近一次运行，还没正常结束时（在跑、在等重试，或最终失败：界面显示错误与「重试」）；正常结束的为 null
    pub run: Option<RunRecord>,
    /// 这个会话开出的 thread
    pub threads: Vec<SessionOut>,
    /// 正在进行的运行当前这一段的事件（与 SSE 的 run 事件同一形状），让中途打开页面也能从这一段的开头看起；没有则为 null
    pub live: Option<LiveSegment>,
    /// 工作状态（与会话列表同一条规则）：不是 idle 前端就显示停止
    pub state: WorkState,
    /// 在等的东西：后台任务、委派、到点唤醒、提问、等批准
    pub waits: Vec<WaitOut>,
    pub context: ContextOut,
    /// 这个会话名下全部运行的累计用量；一次运行的用量在每次模型回复后记上
    pub usage: UsageTotals,
}

#[utoipa::path(get, path = "/api/sessions/{session_id}", tag = "sessions",
    responses((status = 200, body = SessionDetail), (status = 404, body = ErrorOut)))]
async fn get_session(
    State(s): State<AppState>,
    Path(session_id): Path<String>,
) -> ApiResult<axum::Json<SessionDetail>> {
    let view = s.services.sessions.detail(&session_id).await?;
    let agent_id = view.session.agent_id.clone();
    let mut outs = session_outs(&s, std::iter::once(view.session).chain(view.threads).collect()).await?;
    let session = outs.remove(0);
    Ok(axum::Json(SessionDetail {
        session,
        items: view.items,
        compactions: view
            .compactions
            .into_iter()
            .map(|c| CompactionOut {
                through_message_id: c.through_message_id,
                summary: c.summary,
                created_at: c.created_at,
            })
            .collect(),
        mail: mail_outs(&s, view.mail).await?,
        run: view.run,
        threads: outs,
        live: view.live,
        state: view.state,
        waits: wait_outs(&s, &agent_id, view.waits).await?,
        context: ContextOut { tokens: view.context_tokens, threshold_tokens: view.compaction_threshold_tokens },
        usage: s.services.usage.session_totals(&session_id).await?,
    }))
}

#[derive(Deserialize, ToSchema)]
pub struct ReplyIn {
    #[serde(default)]
    pub prompt: String,
    #[serde(default)]
    pub attachments: Vec<String>,
    /// 对挂起提问的回答（条目里 ask.status 为 pending 的提问）；只回答时 prompt 可以为空
    #[serde(default)]
    pub answers: BTreeMap<String, Vec<QuestionAnswer>>,
    /// 对等批准的调用的决定（条目里 approval 为 pending 的工具调用）：approve 批准、deny 拒绝、always 始终允许（批准，以后
    /// 同样的调用不再问）；只做决定时 prompt 可以为空
    #[serde(default)]
    pub approvals: BTreeMap<String, Choice>,
}

/// 在会话里发一条消息（续写），或只回答提问、只批准调用。返回信进的会话。
#[utoipa::path(post, path = "/api/sessions/{session_id}/messages", tag = "sessions", request_body = ReplyIn,
    responses((status = 202, body = SessionOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)))]
async fn reply(
    State(s): State<AppState>,
    Path(session_id): Path<String>,
    Json(body): Json<ReplyIn>,
) -> ApiResult<(StatusCode, axum::Json<SessionOut>)> {
    let session = s
        .services
        .sessions
        .receive(UserMessage {
            content: body.prompt,
            session: UserTarget::Existing(session_id),
            attachments: body.attachments,
            answers: body.answers,
            approvals: body.approvals,
        })
        .await?;
    Ok((StatusCode::ACCEPTED, axum::Json(session_out(&s, session).await?)))
}

/// 停止这个会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作。返回时已生效。
#[utoipa::path(post, path = "/api/sessions/{session_id}/stop", tag = "sessions",
    responses((status = 202), (status = 404, body = ErrorOut)))]
async fn stop(State(s): State<AppState>, Path(session_id): Path<String>) -> ApiResult<StatusCode> {
    s.services.sessions.stop(&session_id).await?;
    Ok(StatusCode::ACCEPTED)
}

/// 手动压缩上下文：排在正在进行的运行之后，在后台做；做完界面随 changed 事件刷新。没有可压缩的（都在保留的最近几步里）
/// 时什么都不变；失败记在服务日志里。
#[utoipa::path(post, path = "/api/sessions/{session_id}/compact", tag = "sessions",
    responses((status = 202), (status = 404, body = ErrorOut)))]
async fn compact(State(s): State<AppState>, Path(session_id): Path<String>) -> ApiResult<StatusCode> {
    s.services.sessions.get(&session_id).await?;
    let sessions = s.services.sessions.clone();
    tokio::spawn(async move {
        match sessions.compact(&session_id).await {
            Ok(steps) => tracing::info!(session_id, steps, "manual compaction done"),
            Err(e) => tracing::error!(session_id, error = %e, "manual compaction failed"),
        }
    });
    Ok(StatusCode::ACCEPTED)
}

/// 重试最近一次最终失败的运行（同一次运行接着跑）。会话在跑、或最近一次运行没有失败时 409。
#[utoipa::path(post, path = "/api/sessions/{session_id}/retry", tag = "sessions",
    responses((status = 202), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)))]
async fn retry(State(s): State<AppState>, Path(session_id): Path<String>) -> ApiResult<StatusCode> {
    s.services.sessions.retry(&session_id).await?;
    Ok(StatusCode::ACCEPTED)
}

/// 标记会话已读：会话的未读就是指向它的未读通知，由通知领域标为已读（幂等，会话不存在时什么都不做）。
#[utoipa::path(post, path = "/api/sessions/{session_id}/read", tag = "sessions", responses((status = 204)))]
async fn mark_read(State(s): State<AppState>, Path(session_id): Path<String>) -> ApiResult<StatusCode> {
    s.services.notifications.mark_session_read(&session_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 删除会话（它开出的 thread 保留）；会话忙时 409。
#[utoipa::path(delete, path = "/api/sessions/{session_id}", tag = "sessions",
    responses((status = 204), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)))]
async fn delete_session(State(s): State<AppState>, Path(session_id): Path<String>) -> ApiResult<StatusCode> {
    s.services.sessions.delete(&session_id).await?;
    Ok(StatusCode::NO_CONTENT)
}
