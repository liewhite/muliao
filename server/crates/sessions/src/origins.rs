//! 会话来源：会话创建时定下、整个会话不变（`sessions.origin` 与 `origin_ref` 两列），以及每种来源在指令里怎么向 agent
//! 说明。来源编号的格式只在这里写：对外只有结构化的 [`SessionOrigin`]，读回只经 [`SessionOrigin::parse`]。
//!
//! 会话领域只写自己开出的来源的说明（[`OWN_NOTES`]：用户、委派）；触发器、issue 的说明含它们自己的规则，由各自领域给出，
//! 组合根合成 [`OriginNotes`]。

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use muliao_platform::errors::{Result, broken};
use muliao_platform::ids::SessionId;

/// 来源的种类：用户在界面发起、触发器、agent 之间委派、issue 看板。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Origin {
    User,
    Trigger,
    Delegation,
    Issue,
}

muliao_platform::serde_names!(Origin);

impl Origin {
    pub const ALL: [Origin; 4] = [Origin::User, Origin::Trigger, Origin::Delegation, Origin::Issue];

    /// 续写规则只在这里：用户能在任何会话里续写；其余来源只续写自己发起的。
    pub(crate) fn writes_anywhere(self) -> bool {
        matches!(self, Origin::User)
    }
}

/// 有自己身份的发送方（别的领域）：经 [`crate::inbound::SenderMail`] 投信、能按身份定位会话。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Sender {
    Trigger { trigger_id: i64 },
    Issue { issue_id: i64 },
}

impl Sender {
    pub(crate) fn origin(self) -> Origin {
        match self {
            Sender::Trigger { .. } => Origin::Trigger,
            Sender::Issue { .. } => Origin::Issue,
        }
    }

    /// 在库里的编号（`origin_ref`）
    pub(crate) fn reference(self) -> String {
        match self {
            Sender::Trigger { trigger_id: id } | Sender::Issue { issue_id: id } => id.to_string(),
        }
    }
}

/// 会话的来源与发起方（界面按 `kind` 区分）。委派开出的是 thread，带 thread 名与发起它的父会话（父会话删除后为空）。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SessionOrigin {
    User,
    Delegation { thread: String, parent_session_id: Option<SessionId> },
    Trigger { trigger_id: i64 },
    Issue { issue_id: i64 },
}

impl SessionOrigin {
    pub fn origin(&self) -> Origin {
        match self {
            SessionOrigin::User => Origin::User,
            SessionOrigin::Delegation { .. } => Origin::Delegation,
            SessionOrigin::Trigger { .. } => Origin::Trigger,
            SessionOrigin::Issue { .. } => Origin::Issue,
        }
    }

    pub(crate) fn sender(&self) -> Option<Sender> {
        match *self {
            SessionOrigin::Trigger { trigger_id } => Some(Sender::Trigger { trigger_id }),
            SessionOrigin::Issue { issue_id } => Some(Sender::Issue { issue_id }),
            SessionOrigin::User | SessionOrigin::Delegation { .. } => None,
        }
    }

    /// 存下的四列（来源、发起方编号、thread 名、父会话）的唯一解析；不合格式是数据坏了。父会话删除后列为空，所以
    /// 委派会话的父会话可以为空，别的会话一定为空。
    pub(crate) fn parse(
        origin: Origin,
        reference: Option<&str>,
        thread: Option<String>,
        parent: Option<SessionId>,
    ) -> Result<Self> {
        let number = || -> Result<i64> {
            reference
                .and_then(|r| r.parse().ok())
                .ok_or_else(|| broken(format!("a {} origin needs a numeric ref, got {reference:?}", origin)))
        };
        Ok(match origin {
            Origin::User | Origin::Delegation if reference.is_some() => {
                return Err(broken(format!("a {} origin has no ref, got {reference:?}", origin)));
            }
            Origin::Delegation => match thread {
                Some(thread) => SessionOrigin::Delegation { thread, parent_session_id: parent },
                None => return Err(broken("a delegation session has no thread name".to_owned())),
            },
            _ if thread.is_some() || parent.is_some() => {
                return Err(broken(format!(
                    "a {} session has a thread name {thread:?} or a parent {parent:?}",
                    origin
                )));
            }
            Origin::User => SessionOrigin::User,
            Origin::Trigger => SessionOrigin::Trigger { trigger_id: number()? },
            Origin::Issue => SessionOrigin::Issue { issue_id: number()? },
        })
    }

    /// 委派开出的 thread 的名字；别的会话没有。
    pub fn thread(&self) -> Option<&str> {
        match self {
            SessionOrigin::Delegation { thread, .. } => Some(thread),
            _ => None,
        }
    }
}

impl From<Sender> for SessionOrigin {
    fn from(s: Sender) -> Self {
        match s {
            Sender::Trigger { trigger_id } => SessionOrigin::Trigger { trigger_id },
            Sender::Issue { issue_id } => SessionOrigin::Issue { issue_id },
        }
    }
}

/// 会话领域自己开出的来源的说明。
pub const OWN_NOTES: [(Origin, &str); 2] = [
    (Origin::User, "The user started this conversation in the app and may be reading along."),
    (Origin::Delegation, "Another agent opened this conversation with you."),
];

/// 每种来源的会话在指令里的那一行说明（会话级的稳定内容，进缓存前缀）。构造时必须覆盖全部来源。
#[derive(Debug, Clone)]
pub struct OriginNotes(HashMap<Origin, String>);

impl OriginNotes {
    /// notes：别的领域给的说明（触发器、issue），与 [`OWN_NOTES`] 合起来要覆盖全部来源，否则 panic（组合根漏装配）。
    pub fn new(notes: impl IntoIterator<Item = (Origin, String)>) -> Self {
        let mut all: HashMap<Origin, String> = OWN_NOTES.iter().map(|(o, n)| (*o, (*n).to_owned())).collect();
        for (o, n) in notes {
            assert!(all.insert(o, n).is_none(), "two session notes for origin {o}");
        }
        let missing: Vec<&Origin> = Origin::ALL.iter().filter(|o| !all.contains_key(o)).collect();
        assert!(missing.is_empty(), "no session note for origins {missing:?}");
        Self(all)
    }

    pub(crate) fn of(&self, origin: Origin) -> &str {
        &self.0[&origin]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stored_origins_round_trip_and_bad_ones_are_reported() {
        let issue = SessionOrigin::Issue { issue_id: 7 };
        assert_eq!(SessionOrigin::parse(Origin::Issue, Some("7"), None, None).unwrap(), issue);
        assert_eq!(SessionOrigin::parse(Origin::User, None, None, None).unwrap(), SessionOrigin::User);
        let parent = Some(SessionId::new("p"));
        let thread = SessionOrigin::Delegation { thread: "t".into(), parent_session_id: parent.clone() };
        assert_eq!(SessionOrigin::parse(Origin::Delegation, None, Some("t".into()), parent.clone()).unwrap(), thread);
        assert!(SessionOrigin::parse(Origin::User, None, None, parent).is_err());
        assert!(SessionOrigin::parse(Origin::User, Some("1"), None, None).is_err());
        assert!(SessionOrigin::parse(Origin::Trigger, None, None, None).is_err());
        assert_eq!(serde_json::to_value(issue).unwrap(), serde_json::json!({"kind": "issue", "issue_id": 7}));
    }
}
