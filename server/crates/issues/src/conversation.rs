//! issue 会话的身份：每个 (issue, agent) 唯一的一个会话（#43），不存会话 id，由会话领域按发送方（`Sender::Issue`）与
//! agent 定位。issue 只交出自己的编号，来源编号的格式归会话领域。

use muliao_platform::ids::{AgentId, IssueId, SessionId};
use std::collections::BTreeMap;

use muliao_sessions::activity::{WorkState, keyed_states_in};
use muliao_sessions::directory::{Session, keyed_among, keyed_in};
use muliao_sessions::inbound::{SenderMail, SenderTarget};
use muliao_sessions::origins::Sender;
use rusqlite::Connection;

use muliao_platform::errors::Result;

fn sender(issue_id: IssueId) -> Sender {
    Sender::Issue { issue_id }
}

/// 投进 agent 在这个 issue 上的会话的信，没有会话时以 title 新建。
pub(crate) fn mail(issue_id: IssueId, agent_id: &AgentId, title: String, content: String) -> SenderMail {
    SenderMail {
        sender: sender(issue_id),
        content,
        session: SenderTarget::Keyed { agent_id: agent_id.to_owned(), title },
    }
}

/// agent 在这个 issue 上的会话；还没有（或已删除）为 None。
pub(crate) fn session_in(c: &Connection, issue_id: IssueId, agent_id: &AgentId) -> Result<Option<Session>> {
    keyed_in(c, sender(issue_id), agent_id)
}

/// 每个 issue -> {agent id -> 它在这个 issue 上的会话 id}。
pub(crate) fn sessions_among(
    c: &Connection,
    issue_ids: &[IssueId],
) -> Result<BTreeMap<IssueId, BTreeMap<AgentId, SessionId>>> {
    let senders: Vec<(IssueId, Sender)> = issue_ids.iter().map(|&i| (i, sender(i))).collect();
    let mut out: BTreeMap<IssueId, BTreeMap<AgentId, SessionId>> =
        issue_ids.iter().map(|&i| (i, BTreeMap::new())).collect();
    for ((issue_id, agent), session) in keyed_among(c, &senders)? {
        out.entry(issue_id).or_default().insert(agent, session.id);
    }
    Ok(out)
}

/// 每个 (issue, agent) 的会话在跑 / 在等 / 空闲；还没有这个会话时为 None。
pub(crate) fn states_in(
    c: &Connection,
    keys: &[(IssueId, AgentId)],
) -> Result<BTreeMap<(IssueId, AgentId), Option<WorkState>>> {
    let asked: Vec<(Sender, AgentId)> = keys.iter().map(|(i, a)| (sender(*i), a.clone())).collect();
    let states = keyed_states_in(c, &asked)?;
    Ok(keys.iter().zip(asked).map(|(k, a)| (k.clone(), states[&a])).collect())
}
