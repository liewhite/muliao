//! 删除会话的唯一规则，与删除、归档 agent 流程里会话领域的一步（docs/architecture.md 第 7、10 节）。

use muliao_platform::ids::SessionId;
use std::sync::Arc;

use muliao_assets::lifecycle::{ArchiveBlocker, DeletionHook};
use muliao_platform::db::Tx;

use crate::activity::{blocking_deletion_in, blocks_agent_in};
use crate::actor::Waker;
use crate::directory::{self, Session};
use crate::events::{Events, SessionDeleted};
use crate::settlement::settle_deleted_in;
use muliao_platform::errors::{Result, conflict};

/// 删除会话（删会话、删 agent 都经过这里）：一个事务。它开出的 thread 保留为独立会话（父引用置空）。任一被删会话忙时拒绝
/// （避免运行结果无处提交、工具写进被删的目录）；交给被删会话、还没结清的委派以「被删除」交回发起方；发布 SessionDeleted。
pub(crate) fn delete_sessions_in(tx: &mut Tx<'_>, waker: &Waker, events: &Events, doomed: &[Session]) -> Result<()> {
    let ids: Vec<SessionId> = doomed.iter().map(|s| s.id.clone()).collect();
    if let Some(busy) = blocking_deletion_in(tx, &ids)?.first() {
        // 错误也会交还给模型，不带 id；定位信息记在日志里
        tracing::info!(session_id = %busy, "session deletion refused: busy");
        return Err(conflict(
            "a session to be deleted is busy (a run is going or queued, or background jobs or delegated work are still \
             going); stop it first, then delete",
        ));
    }
    for s in doomed {
        settle_deleted_in(tx, waker, s)?;
    }
    directory::delete_in(tx, &ids)?;
    for s in doomed {
        events.session_deleted(tx, &SessionDeleted { session_id: s.id.clone(), agent_id: s.agent_id.clone() })?;
    }
    tracing::info!(session_ids = ?ids, "sessions deleted");
    Ok(())
}

/// 删除 agent 时：在标记它的事务里删掉它的全部会话；有忙的就 Conflict，整个删除回滚。
pub fn deletion_hook(waker: Waker, events: Arc<Events>) -> DeletionHook {
    Arc::new(move |tx, agent_id| {
        let sessions = directory::of_agent_in(tx, agent_id)?;
        delete_sessions_in(tx, &waker, &events, &sessions)?;
        tracing::info!(agent = %agent_id, sessions = sessions.len(), "sessions of a deleted agent removed");
        Ok(())
    })
}

/// 归档前的检查：它的会话里有忙的（与删除 agent 时的判断同一条规则）。
pub fn archive_blocker() -> (String, ArchiveBlocker) {
    ("sessions".to_owned(), Arc::new(blocks_agent_in))
}
