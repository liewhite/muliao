//! 会话：列表、新建、会话页、发消息（含回答提问与批准调用）、停止、手动压缩、重试最终失败的运行、标记已读、删除。

use muliao_platform::ids::{AgentId, JobId, MailId, SessionId};
use std::collections::{BTreeMap, BTreeSet};

use axum::extract::State;
use axum::http::StatusCode;
use chrono::{DateTime, Utc};
use muliao_sessions::activity::{Wait, WorkState};
use muliao_sessions::answers::{CallResult, Choice, QuestionAnswer};
use muliao_sessions::directory::{Listing, Session};
use muliao_sessions::inbound::{UserMessage, UserTarget};
use muliao_sessions::live::{LiveSegment, Retrying};
use muliao_sessions::mailbox::{Mail, MailBody, MailState};
use muliao_sessions::origins::{Origin, Sender, SessionOrigin};
use muliao_sessions::runs::RunRecord;
use muliao_sessions::transcript::SavedItem;
use muliao_sessions::work::EventSource;
use muliao_usage::UsageTotals;
use muliao_workspace::jobs::JobStatus;
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
        .routes(routes!(mark_session_read))
}

#[derive(Serialize, ToSchema)]
pub struct SessionOut {
    pub id: SessionId,
    pub agent_id: AgentId,
    pub title: String,
    /// 会话的来源与发起方（结构化，前端不认识库里的编号格式）；委派开出的 thread 带名字与父会话
    pub origin: SessionOrigin,
    /// 有指向它的未读通知（用户在等的回答、提问、agent 主动通知、运行失败）：未读不另存，从通知派生
    pub unread: bool,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

impl SessionOut {
    fn of(s: Session, unread: &BTreeSet<SessionId>) -> Self {
        SessionOut {
            unread: unread.contains(&s.id),
            id: s.id,
            agent_id: s.agent_id,
            title: s.title,
            origin: s.origin,
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
    pub agent_id: Option<AgentId>,
    /// 只列这些来源的会话（可以重复给）
    #[serde(default)]
    #[param(value_type = Option<Vec<String>>)]
    pub origin: Option<Vec<Origin>>,
    /// 只列这个触发器发起的会话（不能再给 origin）
    pub trigger_id: Option<i64>,
    /// 关键字：只列标题或消息含有它的会话（不区分 ASCII 大小写；空串不过滤）
    pub q: Option<String>,
    /// 一页的条数，不给取默认
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
    let limit = q.limit.unwrap_or(muliao_sessions::service::LIST_DEFAULT);
    let listing = Listing { agent_id: q.agent_id, origins: q.origin, sender, keyword: q.q };
    let listed = s.services.sessions.list(listing, limit, q.offset.unwrap_or(0)).await?;
    let states: Vec<WorkState> = listed.iter().map(|x| x.state).collect();
    let outs = session_outs(&s, listed.into_iter().map(|x| x.session).collect()).await?;
    Ok(axum::Json(outs.into_iter().zip(states).map(|(session, state)| SessionListItem { session, state }).collect()))
}

#[derive(Deserialize, ToSchema)]
pub struct StartIn {
    pub agent_id: AgentId,
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
        .receive(UserMessage::Message {
            content: body.prompt,
            session: UserTarget::NewChat { agent_id: body.agent_id },
            attachments: body.attachments,
        })
        .await?;
    Ok((StatusCode::CREATED, axum::Json(session_out(&s, session).await?)))
}

/// 会话信箱里的一封信件（显示在历史之后：排队中的，以及最近一次停止丢弃的）。
#[derive(Serialize, ToSchema)]
pub struct MailOut {
    pub id: MailId,
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
    let mut ids: Vec<String> = mail.iter().flat_map(|m| m.body.attachments().iter().cloned()).collect();
    ids.sort();
    ids.dedup();
    let files: BTreeMap<String, FileOut> =
        s.services.files.referenced(ids).await?.iter().map(|f| (f.id.clone(), FileOut::of(f))).collect();
    Ok(mail
        .into_iter()
        .map(|m| {
            let attachments = m.body.attachments().iter().map(|id| files[id].clone()).collect();
            let (content, kind) = match m.body {
                MailBody::Message { content, .. } => (content, MailKindOut::Message),
                MailBody::Event { source, content, check_in } => (content, MailKindOut::Event { source, check_in }),
                MailBody::Answer { answers, .. } => (String::new(), MailKindOut::Answer { answers }),
            };
            MailOut { attachments, id: m.id, content, state: m.state, created_at: m.created_at, kind }
        })
        .collect())
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
    /// 取等待项与取任务信息不在同一事务，之间任务可能已经结束（结局还没交回）
    pub status: JobStatus,
    /// 日志，相对 agent 工作目录
    pub log_path: String,
    /// 日志最后几行
    pub tail: String,
}

/// 会话在等的一项东西（会话领域的 [`Wait`]）；后台任务另带任务的详情。
#[derive(Serialize, ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum WaitOut {
    /// 后台任务（run_shell background）
    Job { job_id: JobId, since: DateTime<Utc>, job: JobOut },
    /// 委派出去的工作（ask_agent，或 delegate_task 带 report_back）
    Delegation { thread_session_id: SessionId, check_seconds: Option<f64>, since: DateTime<Utc> },
    /// 自己约的到点唤醒
    Wake { due_at: DateTime<Utc>, note: String, since: DateTime<Utc> },
    /// 用户回答提问
    Ask { call_id: String, headers: Vec<String> },
    /// 用户批准一次工具调用
    Approval { call_id: String, tool_name: String },
}

/// WaitOut 的唯一构造入口：后台任务向工作区领域取命令与日志。
async fn wait_outs(s: &AppState, agent_id: &AgentId, waits: Vec<Wait>) -> ApiResult<Vec<WaitOut>> {
    let mut out = Vec::with_capacity(waits.len());
    for wait in waits {
        out.push(match wait {
            Wait::Job { job_id, since } => {
                let info = s.services.jobs.info(job_id, agent_id).await?;
                let job = JobOut {
                    command: info.command,
                    status: info.state.status(),
                    log_path: info.log_path,
                    tail: info.tail,
                };
                WaitOut::Job { job_id, since, job }
            }
            Wait::Delegation { thread_session_id, check_seconds, since } => {
                WaitOut::Delegation { thread_session_id, check_seconds, since }
            }
            Wait::Wake { due_at, note, since } => WaitOut::Wake { due_at, note, since },
            Wait::Ask { call_id, headers } => WaitOut::Ask { call_id, headers },
            Wait::Approval { call_id, tool_name } => WaitOut::Approval { call_id, tool_name },
        });
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
    pub items: Vec<SavedItem>,
    /// 压缩点：through_message_id 及之前的消息已由 summary 代替发给模型（原文仍完整展示）
    pub compactions: Vec<CompactionOut>,
    /// 显示在历史之后的信件：排队中的（处理中的用户消息、已提交的回答），以及最近一次停止丢弃的
    pub mail: Vec<MailOut>,
    /// 最近一次运行，还没正常结束时（在跑、在等重试，或最终失败：界面显示错误与「重试」）；正常结束的为 null
    pub run: Option<RunRecord>,
    /// 这个会话开出的 thread
    pub threads: Vec<ThreadOut>,
    /// 正在进行的运行当前这一段的事件（与 SSE 的 run 事件同一形状），让中途打开页面也能从这一段的开头看起；没有则为 null
    pub live: Option<LiveSegment>,
    /// 这次运行出过暂时性的错、正在自动重试（只在内存里）：上次的错误与下次尝试的时间；没有则为 null
    pub retrying: Option<Retrying>,
    /// 工作状态（与会话列表同一条规则）：不是 idle 前端就显示停止
    pub state: WorkState,
    /// 在等的东西：后台任务、委派、到点唤醒、提问、等批准
    pub waits: Vec<WaitOut>,
    pub context: ContextOut,
    /// 这个会话名下全部运行的累计用量；一次运行的用量在每次模型回复后记上
    pub usage: UsageTotals,
}

/// 会话开出的一个 thread：子会话与它的名字（与 origin 里的同一个，列 thread 时不用再按来源收窄）。
#[derive(Serialize, ToSchema)]
pub struct ThreadOut {
    #[serde(flatten)]
    pub session: SessionOut,
    pub thread: String,
}

#[utoipa::path(get, path = "/api/sessions/{session_id}", tag = "sessions",
    responses((status = 200, body = SessionDetail), (status = 404, body = ErrorOut)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn get_session(
    State(s): State<AppState>,
    Path(session_id): Path<SessionId>,
) -> ApiResult<axum::Json<SessionDetail>> {
    let view = s.services.sessions.detail(&session_id).await?;
    let agent_id = view.session.agent_id.clone();
    let (names, children): (Vec<String>, Vec<Session>) = view.threads.into_iter().map(|t| (t.name, t.session)).unzip();
    let mut outs = session_outs(&s, std::iter::once(view.session).chain(children).collect()).await?;
    let session = outs.remove(0);
    let threads = outs.into_iter().zip(names).map(|(session, thread)| ThreadOut { session, thread }).collect();
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
        threads,
        live: view.live,
        retrying: view.retrying,
        state: view.state,
        waits: wait_outs(&s, &agent_id, view.waits).await?,
        context: ContextOut { tokens: view.context_tokens, threshold_tokens: view.compaction_threshold_tokens },
        usage: s.services.usage.session_totals(&session_id).await?,
    }))
}

/// 用户在会话里发来的一件事，按种类分开。
#[derive(Deserialize, ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ReplyIn {
    /// 一条消息（续写）：文字与附件不能都为空
    Message {
        prompt: String,
        /// 先经 POST /api/files 上传得到的 id
        #[serde(default)]
        attachments: Vec<String>,
    },
    /// 回答一次挂起的提问（条目里 ask.status 为 pending 的提问）：每题一个回答
    Answer { call_id: String, answers: Vec<QuestionAnswer> },
    /// 对一次等批准的调用（条目里 approval 为 pending）的决定：approve 批准、deny 拒绝、always 始终允许（批准，以后同样的
    /// 调用不再问）
    Approval { call_id: String, choice: Choice },
}

/// 在会话里发一条消息（续写）、回答一次提问，或批准一次调用。返回信进的会话。
#[utoipa::path(post, path = "/api/sessions/{session_id}/messages", tag = "sessions", request_body = ReplyIn,
    responses((status = 202, body = SessionOut), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut),
        (status = 422, body = ErrorOut)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn reply(
    State(s): State<AppState>,
    Path(session_id): Path<SessionId>,
    Json(body): Json<ReplyIn>,
) -> ApiResult<(StatusCode, axum::Json<SessionOut>)> {
    let message = match body {
        ReplyIn::Message { prompt, attachments } => {
            UserMessage::Message { session: UserTarget::Existing(session_id), content: prompt, attachments }
        }
        ReplyIn::Answer { call_id, answers } => UserMessage::Answer { session_id, call_id, answers },
        ReplyIn::Approval { call_id, choice } => UserMessage::Approval { session_id, call_id, choice },
    };
    let session = s.services.sessions.receive(message).await?;
    Ok((StatusCode::ACCEPTED, axum::Json(session_out(&s, session).await?)))
}

/// 停止这个会话：结束正在进行的运行（已经产生的部分照常保存）、清空信箱，连带停下它发起的异步工作。返回时已生效。
#[utoipa::path(post, path = "/api/sessions/{session_id}/stop", tag = "sessions",
    responses((status = 202), (status = 404, body = ErrorOut)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn stop(State(s): State<AppState>, Path(session_id): Path<SessionId>) -> ApiResult<StatusCode> {
    s.services.sessions.stop(&session_id).await?;
    Ok(StatusCode::ACCEPTED)
}

/// 手动压缩上下文：排在正在进行的运行之后，在后台做；开始与做完时会话页随 saved 事件刷新。没有可压缩的（都在保留的最近几步里）
/// 时什么都不变；失败记在服务日志里。
#[utoipa::path(post, path = "/api/sessions/{session_id}/compact", tag = "sessions",
    responses((status = 202), (status = 404, body = ErrorOut)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn compact(State(s): State<AppState>, Path(session_id): Path<SessionId>) -> ApiResult<StatusCode> {
    s.services.sessions.get(&session_id).await?;
    let sessions = s.services.sessions.clone();
    tokio::spawn(async move {
        match sessions.compact(&session_id).await {
            Ok(steps) => tracing::info!(%session_id, steps, "manual compaction done"),
            Err(e) => tracing::error!(%session_id, error = %e, "manual compaction failed"),
        }
    });
    Ok(StatusCode::ACCEPTED)
}

/// 重试最近一次最终失败的运行（同一次运行接着跑）。会话在跑、或最近一次运行没有失败时 409。
#[utoipa::path(post, path = "/api/sessions/{session_id}/retry", tag = "sessions",
    responses((status = 202), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn retry(State(s): State<AppState>, Path(session_id): Path<SessionId>) -> ApiResult<StatusCode> {
    s.services.sessions.retry(&session_id).await?;
    Ok(StatusCode::ACCEPTED)
}

/// 标记会话已读：会话的未读就是指向它的未读通知，由通知领域标为已读（幂等，会话不存在时什么都不做）。
#[utoipa::path(post, path = "/api/sessions/{session_id}/read", tag = "sessions", responses((status = 204)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn mark_session_read(State(s): State<AppState>, Path(session_id): Path<SessionId>) -> ApiResult<StatusCode> {
    s.services.notifications.mark_session_read(&session_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

/// 删除会话（它开出的 thread 保留）；会话忙时 409。
#[utoipa::path(delete, path = "/api/sessions/{session_id}", tag = "sessions",
    responses((status = 204), (status = 404, body = ErrorOut), (status = 409, body = ErrorOut)),
    params(("session_id" = SessionId, Path, description = "会话的编号")))]
async fn delete_session(State(s): State<AppState>, Path(session_id): Path<SessionId>) -> ApiResult<StatusCode> {
    s.services.sessions.delete(&session_id).await?;
    Ok(StatusCode::NO_CONTENT)
}
