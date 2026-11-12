//! skill 脚本的「始终允许」（#531）：用户批准一次等批准的 skill_run 调用时选「始终允许」，之后同一个调用直接执行，不再停下
//! 等批准。「同一个调用」：同一个 agent（按 id，改名不丢）、同一 skill 的同一脚本、同样的参数与注入的 secret，且 skill 目录里
//! 除 SKILL.md 外的内容没变（改了脚本或它用的辅助文件就要重新批准，改说明不用）。timeout、background、heartbeat_seconds
//! 不算在内。没有通配与前缀匹配。
//!
//! 授权存在 script_grants 表（不放资源目录：agent 能写自己的资源目录）。用户在设置页查看、撤销；点「始终允许」不会顺带放行
//! 别的会话里已经在等的同一调用（那些各自等用户的决定）。

use std::sync::Arc;

use chrono::{DateTime, Utc};
use muliao_assets::lifecycle::DeletionHook;
use muliao_platform::db::Database;
use rusqlite::{OptionalExtension, params};
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::error::{Result, not_found};

/// 按始终允许直接执行时，接在结果前面告诉模型（也留在会话记录里）
pub const GRANTED_NOTE: &str = "(Run without asking: the user always allows this exact call.)";

/// 一次 skill_run 调用里决定「是不是同一个调用」的部分。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScriptCall {
    pub agent_id: String,
    pub skill: String,
    /// 相对 skill 目录的脚本路径（解析后的）
    pub script: String,
    pub args: Vec<String>,
    /// 排序去重：secret 的顺序与重复不影响注入的结果
    pub secrets: Vec<String>,
    /// skill 目录的内容指纹（`SkillScripts::fingerprint`）
    pub fingerprint: String,
}

impl ScriptCall {
    pub fn new(
        agent_id: &str,
        skill: &str,
        script: &str,
        args: &[String],
        secrets: &[String],
        fingerprint: &str,
    ) -> Self {
        let mut secrets = secrets.to_vec();
        secrets.sort();
        secrets.dedup();
        Self {
            agent_id: agent_id.to_owned(),
            skill: skill.to_owned(),
            script: script.to_owned(),
            args: args.to_vec(),
            secrets,
            fingerprint: fingerprint.to_owned(),
        }
    }

    /// 查找与去重的键：与 Python 版逐字节相同（`json.dumps([...], ensure_ascii=False)` 的 sha256），迁过来的授权照样认。
    pub fn key(&self) -> String {
        let s = |v: &str| serde_json::to_string(v).expect("strings serialize");
        let list = |v: &[String]| format!("[{}]", v.iter().map(|x| s(x)).collect::<Vec<_>>().join(", "));
        let data = format!(
            "[{}, {}, {}, {}, {}, {}]",
            s(&self.agent_id),
            s(&self.skill),
            s(&self.script),
            list(&self.args),
            list(&self.secrets),
            s(&self.fingerprint)
        );
        format!("{:x}", Sha256::digest(data.as_bytes()))
    }
}

/// 设置页看到的一条始终允许。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct GrantInfo {
    pub id: i64,
    pub agent_id: String,
    pub skill: String,
    pub script: String,
    pub args: Vec<String>,
    pub secrets: Vec<String>,
    pub session_id: String,
    pub created_at: DateTime<Utc>,
    pub hits: i64,
    pub last_used_at: Option<DateTime<Utc>>,
}

pub struct ScriptGrants {
    db: Arc<Database>,
}

impl ScriptGrants {
    pub fn new(db: Arc<Database>) -> Self {
        Self { db }
    }

    /// 删除 agent 的流程：删除给它的始终允许。
    pub fn deletion_hook() -> DeletionHook {
        Arc::new(|tx, agent_id| {
            tx.execute("DELETE FROM script_grants WHERE agent_id = ?1", [agent_id])?;
            Ok(())
        })
    }

    /// 这个调用有始终允许时返回它的 id。
    pub async fn find(&self, call: &ScriptCall) -> Result<Option<i64>> {
        let key = call.key();
        self.db
            .read(move |c| {
                Ok(c.query_row("SELECT id FROM script_grants WHERE key = ?1", [key], |r| r.get(0)).optional()?)
            })
            .await
    }

    /// 按这条始终允许直接执行了一次。期间被撤销的不算（行已删，什么也不改）。
    pub async fn used(&self, grant_id: i64) -> Result<()> {
        self.db
            .write(move |tx| {
                tx.execute(
                    "UPDATE script_grants SET hits = hits + 1, last_used_at = ?2 WHERE id = ?1",
                    params![grant_id, Utc::now()],
                )?;
                Ok(())
            })
            .await
    }

    /// 用户对这次调用点了「始终允许」。已有同一调用的始终允许时不再添一条。
    pub async fn grant(&self, call: ScriptCall, session_id: &str, call_id: &str) -> Result<()> {
        let (session_id, call_id) = (session_id.to_owned(), call_id.to_owned());
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO script_grants (agent_id, skill, script, args, secrets, fingerprint, key, session_id, \
                     call_id, created_at, hits) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 0) \
                     ON CONFLICT (key) DO NOTHING",
                    params![
                        call.agent_id,
                        call.skill,
                        call.script,
                        serde_json::to_string(&call.args).expect("strings serialize"),
                        serde_json::to_string(&call.secrets).expect("strings serialize"),
                        call.fingerprint,
                        call.key(),
                        session_id,
                        call_id,
                        Utc::now(),
                    ],
                )?;
                Ok(())
            })
            .await
    }

    /// 全部始终允许，新的在前。
    pub async fn list(&self) -> Result<Vec<GrantInfo>> {
        self.db
            .read(|c| {
                let mut stmt = c.prepare(
                    "SELECT id, agent_id, skill, script, args, secrets, session_id, created_at, hits, last_used_at \
                     FROM script_grants ORDER BY id DESC",
                )?;
                let rows = stmt.query_map([], |r| {
                    Ok((
                        GrantInfo {
                            id: r.get(0)?,
                            agent_id: r.get(1)?,
                            skill: r.get(2)?,
                            script: r.get(3)?,
                            args: Vec::new(),
                            secrets: Vec::new(),
                            session_id: r.get(6)?,
                            created_at: r.get(7)?,
                            hits: r.get(8)?,
                            last_used_at: r.get(9)?,
                        },
                        r.get::<_, String>(4)?,
                        r.get::<_, String>(5)?,
                    ))
                })?;
                let mut out = Vec::new();
                for row in rows {
                    let (mut info, args, secrets) = row?;
                    info.args = parse_list(&args)?;
                    info.secrets = parse_list(&secrets)?;
                    out.push(info);
                }
                Ok(out)
            })
            .await
    }

    /// 撤销一条始终允许：之后同一调用重新要批准。
    pub async fn revoke(&self, grant_id: i64) -> Result<()> {
        self.db
            .write(move |tx| {
                if tx.execute("DELETE FROM script_grants WHERE id = ?1", [grant_id])? == 0 {
                    return Err(not_found(format!("script grant {grant_id} not found")));
                }
                Ok(())
            })
            .await
    }
}

fn parse_list(raw: &str) -> Result<Vec<String>> {
    serde_json::from_str(raw).map_err(|e| crate::error::Error::Broken(format!("script grant list {raw:?}: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys_match_python() {
        // python3 -c 'import hashlib,json;print(hashlib.sha256(json.dumps(["a1","s","run.sh",["x y","中"],["K"],"fp"],
        //   ensure_ascii=False).encode()).hexdigest())'
        let call = ScriptCall::new("a1", "s", "run.sh", &["x y".into(), "中".into()], &["K".into(), "K".into()], "fp");
        assert_eq!(call.secrets, ["K"]);
        assert_eq!(call.key(), "b1efe18f12f79a9b6d985472943e68ec69753c11d5597bfbdeff25726a58392e");
    }
}
