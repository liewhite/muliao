//! 界面用的会话门面（HTTP 层只经这里）：会话页、会话列表、发消息、停止、删除、手动压缩、重试最终失败的运行。别的领域
//! 用各模块的 `*_in` 查询（在自己的事务里）与 [`crate::intake::inbound::SenderInbox`]、[`crate::outside::work::SessionWork`]。

use muliao_platform::ids::{CallId, SessionId};
use std::collections::BTreeMap;
use std::sync::Arc;

use crate::Core;
use crate::activity::{Wait, WorkState, statuses_in, waits_of_in};
use crate::deletion::delete_sessions_in;
use crate::directory::{self, Listing, Session, Thread};
use crate::execution::actor::{Actors, ManualCompaction};
use crate::execution::halt::Halted;
use crate::execution::live::{LiveSegment, Retrying, RunEvent};
use crate::execution::runs::{self, RunRecord};
use crate::intake::inbound::{CallReply, Inbound, UserMessage, receive_in, reply_in};
use crate::mailbox::{Mail, visible_in};
use crate::record::compaction::context_tokens;
use crate::record::history::{self, Compaction, context_of};
use crate::record::transcript::{SavedItem, annotate_approvals, annotate_asks, annotate_threads, to_items};
use crate::thread_calls::call_threads_in;
use muliao_platform::errors::{Error, Result, not_found};

/// 会话列表不给条数时一页多少条
pub const LIST_DEFAULT: usize = 50;

/// 会话页看到的会话（读模型）：一次取齐，与实时快照对得上。
#[derive(Debug, Clone)]
pub struct SessionView {
    pub session: Session,
    /// 完整原始历史（不受压缩影响）的展示条目，带来源消息 id 与提问、批准、thread 的状态；正在进行的那一段已经保存的部分
    /// 由实时快照展示，这里不重复
    pub items: Vec<SavedItem>,
    pub compactions: Vec<Compaction>,
    /// 显示在历史之后的信件：排队中的，以及最近一次停止丢弃的
    pub mail: Vec<Mail>,
    /// 最近一次运行：没结束的（在跑或在等重试）或最终失败的（界面显示错误与「重试」）；正常结束的为 None
    pub run: Option<RunRecord>,
    pub threads: Vec<Thread>,
    pub live: Option<LiveSegment>,
    /// 这次运行出过暂时性的错、正在自动重试：上次的错误与下次尝试的时间
    pub retrying: Option<Retrying>,
    /// 工作状态（与会话列表同一条规则）：不是 idle 就显示停止
    pub state: WorkState,
    /// 在等的东西（与 state 同一次计算）
    pub waits: Vec<Wait>,
    /// 发给模型的上下文现在的大小（压缩判断看的同一个数）；还没有回复、或刚压缩过还没有新回复时为 None
    pub context_tokens: Option<u64>,
    pub compaction_threshold_tokens: u64,
}

/// 会话列表的一行：会话与排序时用的工作状态（同一次计算，置顶与标记对得上）。
#[derive(Debug, Clone)]
pub struct ListedSession {
    pub session: Session,
    pub state: WorkState,
}

#[derive(Clone)]
pub struct SessionService {
    core: Arc<Core>,
    actors: Actors,
}

impl SessionService {
    pub(crate) fn new(core: Arc<Core>, actors: Actors) -> Self {
        Self { core, actors }
    }

    /// 用户在界面上发的消息（续写或新建会话），返回信进的会话。
    pub async fn receive(&self, message: UserMessage) -> Result<Session> {
        let waker = self.core.d.waker.clone();
        self.core
            .d
            .db
            .write(move |tx| match message {
                UserMessage::Message { session, content, attachments } => {
                    receive_in(tx, &waker, Inbound::User { session, content, attachments })
                }
                UserMessage::Answer { session_id, call_id, answers } => {
                    reply_in(tx, &waker, &session_id, call_id, CallReply::Answer(answers))
                }
                UserMessage::Approval { session_id, call_id, choice } => {
                    reply_in(tx, &waker, &session_id, call_id, CallReply::Approval(choice))
                }
            })
            .await
    }

    pub async fn get(&self, session_id: &SessionId) -> Result<Session> {
        let id = session_id.to_owned();
        self.core
            .d
            .db
            .read(move |c| directory::get_in(c, &id)?.ok_or_else(|| not_found(format!("session {id:?} not found"))))
            .await
    }

    pub async fn detail(&self, session_id: &SessionId) -> Result<SessionView> {
        // 先取快照再读库：快照这一段已在展示的消息不在历史里重复；之间换了段时，新保存的消息照旧由旧快照展示，saved 事件
        // 随后让前端重新拉取
        let live = self.core.live.snapshot(session_id);
        let retrying = self.core.live.retry_state(session_id);
        let (id, core) = (session_id.to_owned(), self.core.clone());
        self.core
            .d
            .db
            .read(move |c| {
                let session =
                    directory::get_in(c, &id)?.ok_or_else(|| not_found(format!("session {id:?} not found")))?;
                let rows = history::transcript_in(c, &id, None)?;
                let compactions = history::compactions_in(c, &id)?;
                let context = context_of(compactions.last(), &rows);
                let status = statuses_in(c, std::slice::from_ref(&id))?.remove(&id).expect("asked for it");
                let call_threads = call_threads_in(c, &id)?;
                let after = live.as_ref().and_then(|l| l.after_message_id);
                let (mut ids, mut items) = (Vec::new(), Vec::new());
                for row in rows.iter().filter(|r| after.is_none_or(|a| r.id <= a)) {
                    for item in to_items([&row.message], &core.d.renders, Some(row.created_at))? {
                        ids.push(row.id);
                        items.push(item);
                    }
                }
                let asks = &status.waiting.asks;
                annotate_threads(&mut items, &call_threads);
                annotate_asks(&mut items, asks)?;
                annotate_approvals(&mut items, asks);
                let items =
                    ids.into_iter().zip(items).map(|(message_id, item)| SavedItem { message_id, item }).collect();
                let run = runs::latest_in(c, &id)?.filter(|r| r.unfinished() || r.failure().is_some());
                let waits = waits_of_in(c, &id, &status.waiting)?;
                Ok::<_, Error>(SessionView {
                    items,
                    compactions,
                    mail: visible_in(c, &id)?,
                    run,
                    threads: directory::threads_of(c, &id)?,
                    live: live.map(|l| with_threads(l, &call_threads)),
                    retrying,
                    state: status.state,
                    waits,
                    context_tokens: context_tokens(&context.messages, context.stale_responses),
                    compaction_threshold_tokens: core.d.compaction.threshold_tokens,
                    session,
                })
            })
            .await
    }

    /// 会话列表（顶层会话与 thread）：不是 idle 的置顶（看筛选后的全部会话，所以第一页就含全部进行中的），其余按最近更新。
    pub async fn list(&self, listing: Listing, limit: usize, offset: usize) -> Result<Vec<ListedSession>> {
        self.core
            .d
            .db
            .read(move |c| {
                let ids = directory::listed_ids_in(c, &listing)?;
                let states: BTreeMap<SessionId, WorkState> =
                    statuses_in(c, &ids)?.into_iter().map(|(k, s)| (k, s.state)).collect();
                let active: Vec<SessionId> = ids.iter().filter(|x| states[*x] != WorkState::Idle).cloned().collect();
                Ok::<_, Error>(
                    directory::listed_page_in(c, &listing, &active, limit, offset)?
                        .into_iter()
                        .map(|s| ListedSession { state: states[&s.id], session: s })
                        .collect(),
                )
            })
            .await
    }

    /// 用户停止会话：结束正在进行的运行（已产生的部分照常保存）、清空信箱，连带停下它发起的异步工作。返回时已生效。
    /// 会话不存在时 NotFound。
    pub async fn stop(&self, session_id: &SessionId) -> Result<()> {
        match self.actors.stop(session_id).await? {
            Halted::Stopped => Ok(()),
            Halted::Gone => Err(not_found(format!("session {session_id:?} not found"))),
        }
    }

    /// 用户删除会话（规则见 [`crate::deletion`]）：忙时 Conflict。
    pub async fn delete(&self, session_id: &SessionId) -> Result<()> {
        let (id, waker, events) = (session_id.to_owned(), self.core.d.waker.clone(), self.core.d.events.clone());
        self.core
            .d
            .db
            .write(move |tx| {
                let session =
                    directory::get_in(tx, &id)?.ok_or_else(|| not_found(format!("session {id:?} not found")))?;
                delete_sessions_in(tx, &waker, &events, &[session])
            })
            .await
    }

    /// 用户手动压缩上下文，做完或被停止才返回。会话在跑时拒绝，压缩中可以停止（见 actor）。
    pub async fn compact(&self, session_id: &SessionId) -> Result<ManualCompaction> {
        self.actors.compact(session_id).await
    }

    /// 重试最近一次最终失败的运行。
    pub async fn retry(&self, session_id: &SessionId) -> Result<()> {
        self.actors.retry(session_id).await
    }
}

/// 实时快照里的调用同样挂上它开出或续写的 thread：中途打开会话页时这一段的调用也显示它开出的 thread。
fn with_threads(mut live: LiveSegment, call_threads: &BTreeMap<CallId, SessionId>) -> LiveSegment {
    for e in &mut live.events {
        if let RunEvent::Item { item } = &mut e.event {
            annotate_threads(std::slice::from_mut(&mut **item), call_threads);
        }
    }
    live
}
