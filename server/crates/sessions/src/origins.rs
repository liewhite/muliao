//! 会话来源：会话创建时定下、整个会话不变（`sessions.origin` 与 `origin_ref` 两列），以及每种来源在指令里怎么向 agent
//! 说明。来源编号的格式只在这里写：对外只有结构化的 [`SessionOrigin`]，读回只经 [`SessionOrigin::parse`]。
//!
//! 会话领域只写自己开出的来源的说明（[`OWN_NOTES`]：用户、委派）；触发器、issue 的说明含它们自己的规则，由各自领域给出，
//! 组合根合成 [`OriginNotes`]。

use std::collections::HashMap;

use serde::{Deserialize, Serialize};

use muliao_platform::errors::{Result, broken};

/// 来源的种类：用户在界面发起、触发器、agent 之间委派、issue 看板。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Origin {
    User,
    Trigger,
    Delegation,
    Issue,
}

impl Origin {
    pub const ALL: [Origin; 4] = [Origin::User, Origin::Trigger, Origin::Delegation, Origin::Issue];

    pub fn as_str(self) -> &'static str {
        match self {
            Origin::User => "user",
            Origin::Trigger => "trigger",
            Origin::Delegation => "delegation",
            Origin::Issue => "issue",
        }
    }

    pub fn parse(s: &str) -> Result<Self> {
        Origin::ALL.into_iter().find(|o| o.as_str() == s).ok_or_else(|| broken(format!("unknown origin {s:?}")))
    }

    /// 续写规则只在这里：用户能在任何会话里续写；其余来源只续写自己发起的。
    pub fn writes_anywhere(self) -> bool {
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
    pub fn origin(self) -> Origin {
        match self {
            Sender::Trigger { .. } => Origin::Trigger,
            Sender::Issue { .. } => Origin::Issue,
        }
    }

    /// 在库里的编号（`origin_ref`）
    pub fn reference(self) -> String {
        match self {
            Sender::Trigger { trigger_id: id } | Sender::Issue { issue_id: id } => id.to_string(),
        }
    }
}

/// 会话的来源与发起方（界面按 `kind` 区分）。委派开出的是 thread，带 thread 名；发起方是父会话（存在
/// `parent_session_id`，不在这里另存一份）。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum SessionOrigin {
    User,
    Delegation { thread: String },
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

    pub fn sender(&self) -> Option<Sender> {
        match *self {
            SessionOrigin::Trigger { trigger_id } => Some(Sender::Trigger { trigger_id }),
            SessionOrigin::Issue { issue_id } => Some(Sender::Issue { issue_id }),
            SessionOrigin::User | SessionOrigin::Delegation { .. } => None,
        }
    }

    /// 存下的三列（来源、发起方编号、thread 名）的唯一解析；不合格式是数据坏了。
    pub fn parse(origin: &str, reference: Option<&str>, thread: Option<String>) -> Result<Self> {
        let origin = Origin::parse(origin)?;
        let number = || -> Result<i64> {
            reference
                .and_then(|r| r.parse().ok())
                .ok_or_else(|| broken(format!("a {} origin needs a numeric ref, got {reference:?}", origin.as_str())))
        };
        Ok(match origin {
            Origin::User | Origin::Delegation if reference.is_some() => {
                return Err(broken(format!("a {} origin has no ref, got {reference:?}", origin.as_str())));
            }
            Origin::Delegation => match thread {
                Some(thread) => SessionOrigin::Delegation { thread },
                None => return Err(broken("a delegation session has no thread name".to_owned())),
            },
            _ if thread.is_some() => {
                return Err(broken(format!("a {} session has a thread name {thread:?}", origin.as_str())));
            }
            Origin::User => SessionOrigin::User,
            Origin::Trigger => SessionOrigin::Trigger { trigger_id: number()? },
            Origin::Issue => SessionOrigin::Issue { issue_id: number()? },
        })
    }

    /// 委派开出的 thread 的名字；别的会话没有。
    pub fn thread(&self) -> Option<&str> {
        match self {
            SessionOrigin::Delegation { thread } => Some(thread),
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
            assert!(all.insert(o, n).is_none(), "two session notes for origin {}", o.as_str());
        }
        let missing: Vec<&str> = Origin::ALL.iter().filter(|o| !all.contains_key(o)).map(|o| o.as_str()).collect();
        assert!(missing.is_empty(), "no session note for origins {missing:?}");
        Self(all)
    }

    pub fn of(&self, origin: Origin) -> &str {
        &self.0[&origin]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stored_origins_round_trip_and_bad_ones_are_reported() {
        let issue = SessionOrigin::Issue { issue_id: 7 };
        assert_eq!(SessionOrigin::parse("issue", Some("7"), None).unwrap(), issue);
        assert_eq!(SessionOrigin::parse("user", None, None).unwrap(), SessionOrigin::User);
        assert!(SessionOrigin::parse("user", Some("1"), None).is_err());
        assert!(SessionOrigin::parse("trigger", None, None).is_err());
        assert!(SessionOrigin::parse("nope", None, None).is_err());
        assert_eq!(serde_json::to_value(issue).unwrap(), serde_json::json!({"kind": "issue", "issue_id": 7}));
    }
}
