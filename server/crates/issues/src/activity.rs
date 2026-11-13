//! issue 时间线（`issue_activities` 表的唯一读写处）：每条动态是什么（[`Happening`]，每种一个变体，种类、内容 body、变化
//! change 的形状只在这里定义一次），以及记下、读回。只增不改。
//!
//! 库里的三列（kind、body、change）就是 [`Happening`] 按 serde 导出的样子：change 是 `{"from": 原值, "to": 新值}`，上游完成
//! 另带 `"issue"`。HTTP 返回的一条动态是 [`Activity`]，kind、body、change 与共同字段平铺在一起。

use chrono::{DateTime, Utc};
use muliao_platform::ids::{AgentId, SessionId};
use rusqlite::{Connection, Row, params};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::model::{Actor, Assignee, By, Priority, Recorded};
use muliao_platform::errors::{Result, broken};

/// 属性从什么改成什么（存为 `{"from": ..., "to": ...}`）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct Change<T> {
    #[serde(rename = "from")]
    pub before: T,
    pub to: T,
}

impl<T> Change<T> {
    pub fn new(before: T, to: T) -> Self {
        Change { before, to }
    }
}

/// 一个上游完成（done / cancelled）：它的状态变化，状态的取值同 [`Happening::Status`]。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DependencyChange {
    /// 完成的那个上游
    pub issue: i64,
    #[serde(rename = "from")]
    #[schema(value_type = String)]
    pub before: Recorded,
    #[schema(value_type = String)]
    pub to: Recorded,
}

/// 不是属性变化、只有一个值的记法（存为 `{"from": null, "to": 值}`）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct Arrival {
    #[serde(rename = "from")]
    pub before: (),
    pub to: AgentId,
}

/// 时间线上发生的一件事。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Happening {
    Created,
    /// 评论（body 是内容）
    Comment {
        body: String,
    },
    /// 改了说明（不记内容）
    Description,
    Title {
        change: Change<String>,
    },
    /// 状态：#258 之前的历史里有已去掉的 todo、waiting（[`Recorded`]）
    Status {
        #[schema(value_type = Change<String>)]
        change: Change<Recorded>,
    },
    Priority {
        change: Change<Priority>,
    },
    /// agent id、`user`，或 null（未指派）
    Assignee {
        #[schema(value_type = Change<Option<String>>)]
        change: Change<Option<Assignee>>,
    },
    /// agent id，或 null（负责的 agent 已删除）
    Owner {
        change: Change<Option<AgentId>>,
    },
    /// 上游 issue 编号列表的前后
    DependsOn {
        change: Change<Vec<i64>>,
    },
    /// 一个上游完成（done / cancelled）
    Dependency {
        change: DependencyChange,
    },
    /// 做事的 agent 在它这个 issue 的会话里的运行最终失败了（actor 是它）；body 是截短的错误原文
    RunFailed {
        body: String,
    },
    /// 它在这个 issue 会话里的一次运行结束了，issue 却还是 in_progress、没人在做（[`crate::rules::stalled`]）：请它表态。
    /// actor 是该 agent
    Unsettled,
    /// actor 用 issue_stop 停掉了被指派 agent 在这个 issue 上的会话：to 是那个 agent 的 id
    Stopped {
        change: Arrival,
    },
}

impl Happening {
    /// 种类（库里 kind 列与 HTTP 的 kind 字段）。
    pub fn kind(&self) -> &'static str {
        match self {
            Happening::Created => "created",
            Happening::Comment { .. } => "comment",
            Happening::Description => "description",
            Happening::Title { .. } => "title",
            Happening::Status { .. } => "status",
            Happening::Priority { .. } => "priority",
            Happening::Assignee { .. } => "assignee",
            Happening::Owner { .. } => "owner",
            Happening::DependsOn { .. } => "depends_on",
            Happening::Dependency { .. } => "dependency",
            Happening::RunFailed { .. } => "run_failed",
            Happening::Unsettled => "unsettled",
            Happening::Stopped { .. } => "stopped",
        }
    }

    /// 存进库的三列：种类、内容、变化（JSON）。
    fn stored(&self) -> (&'static str, Option<String>, Option<String>) {
        let Value::Object(mut fields) = serde_json::to_value(self).expect("a happening serializes") else {
            unreachable!("an internally tagged enum serializes to an object")
        };
        let body = fields.remove("body").map(|b| b.as_str().expect("body is text").to_owned());
        let change = fields.remove("change").map(|c| c.to_string());
        (self.kind(), body, change)
    }

    /// 库里的三列按种类解析；形状不符（种类缺了内容或变化）当场报错，不交给读的人去猜。
    fn parse(kind: &str, body: Option<String>, change: Option<&str>) -> std::result::Result<Self, String> {
        let mut fields = serde_json::Map::new();
        fields.insert("kind".into(), Value::from(kind));
        if let Some(b) = body {
            fields.insert("body".into(), Value::from(b));
        }
        if let Some(c) = change {
            fields.insert("change".into(), serde_json::from_str(c).map_err(|e| e.to_string())?);
        }
        serde_json::from_value(Value::Object(fields)).map_err(|e| e.to_string())
    }
}

/// 时间线上记下的一条：谁、什么时候、在哪个会话里，发生了什么。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct Activity {
    pub id: i64,
    pub issue_id: i64,
    pub created_at: DateTime<Utc>,
    /// 做这件事的人：agent id；用户为 `user`，系统自己的清理为 `system`
    pub actor: Actor,
    /// 这件事发生在哪个会话；用户在界面上的操作、系统的清理、会话已删除时为 null
    pub session_id: Option<SessionId>,
    #[serde(flatten)]
    pub what: Happening,
}

const COLUMNS: &str = "id, issue_id, created_at, actor, kind, body, change, session_id";

fn activity(r: &Row<'_>) -> rusqlite::Result<std::result::Result<Activity, String>> {
    let id: i64 = r.get(0)?;
    let kind: String = r.get(4)?;
    let change: Option<String> = r.get(6)?;
    let what = match Happening::parse(&kind, r.get(5)?, change.as_deref()) {
        Ok(w) => w,
        Err(e) => return Ok(Err(format!("issue activity {id} ({kind}) does not fit its kind: {e}"))),
    };
    Ok(Ok(Activity {
        id,
        issue_id: r.get(1)?,
        created_at: r.get(2)?,
        actor: Actor::from(r.get::<_, String>(3)?),
        session_id: r.get(7)?,
        what,
    }))
}

/// 记一条动态，返回记下的那一条。
pub(crate) fn record_in(c: &Connection, issue_id: i64, by: &By, what: Happening) -> Result<Activity> {
    let (kind, body, change) = what.stored();
    let now = Utc::now();
    c.execute(
        "INSERT INTO issue_activities (issue_id, created_at, actor, kind, body, change, session_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![issue_id, now, by.actor.as_str(), kind, body, change, by.session_id],
    )?;
    Ok(Activity {
        id: c.last_insert_rowid(),
        issue_id,
        created_at: now,
        actor: by.actor.clone(),
        session_id: by.session_id.clone(),
        what,
    })
}

pub(crate) fn query(c: &Connection, filter: &str, params: impl rusqlite::Params) -> Result<Vec<Activity>> {
    let mut stmt = c.prepare(&format!("SELECT {COLUMNS} FROM issue_activities WHERE {filter}"))?;
    let rows = stmt.query_map(params, activity)?.collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().map(|r| r.map_err(broken)).collect()
}

/// 一个 issue 的整条时间线，按先后。
pub fn timeline_in(c: &Connection, issue_id: i64) -> Result<Vec<Activity>> {
    query(c, "issue_id = ?1 ORDER BY id", [issue_id])
}

/// 时间线上来自被删会话的动态保留，只是不再指向它。
pub(crate) fn forget_session_in(c: &Connection, session_id: &SessionId) -> Result<()> {
    c.execute("UPDATE issue_activities SET session_id = NULL WHERE session_id = ?1", [session_id])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn happenings_round_trip_through_the_stored_columns() {
        let all = [
            Happening::Created,
            Happening::Comment { body: "hi".into() },
            Happening::Description,
            Happening::Status { change: Change::new(String::from("todo").into(), String::from("done").into()) },
            Happening::Assignee { change: Change::new(None, Some(Assignee::User)) },
            Happening::Owner { change: Change::new(Some("abc".into()), None) },
            Happening::DependsOn { change: Change::new(vec![1], vec![]) },
            Happening::Dependency {
                change: DependencyChange {
                    issue: 3,
                    before: String::from("in_progress").into(),
                    to: String::from("done").into(),
                },
            },
            Happening::Stopped { change: Arrival { before: (), to: "abc".into() } },
            Happening::Unsettled,
        ];
        for what in all {
            let (kind, body, change) = what.stored();
            assert_eq!(Happening::parse(kind, body, change.as_deref()).unwrap(), what);
        }
        let (_, _, change) = Happening::Stopped { change: Arrival { before: (), to: "a".into() } }.stored();
        assert_eq!(change.unwrap(), r#"{"from":null,"to":"a"}"#);
        let (_, _, change) = Happening::Assignee { change: Change::new(None, Some(Assignee::User)) }.stored();
        assert_eq!(change.unwrap(), r#"{"from":null,"to":"user"}"#);
        // 形状不符的报出来
        assert!(Happening::parse("comment", None, None).is_err());
        assert!(Happening::parse("title", None, Some(r#"{"from":"a","to":"b","x":1}"#)).is_err());
        assert!(Happening::parse("nope", None, None).is_err());
    }

    #[test]
    fn an_activity_is_flat_in_json() {
        let a = Activity {
            id: 1,
            issue_id: 2,
            created_at: DateTime::from_timestamp(0, 0).unwrap(),
            actor: Actor::User,
            session_id: None,
            what: Happening::Comment { body: "b".into() },
        };
        let v = serde_json::to_value(&a).unwrap();
        assert_eq!(v["kind"], "comment");
        assert_eq!(v["body"], "b");
        assert_eq!(v["actor"], "user");
    }
}
