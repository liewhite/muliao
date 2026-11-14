//! issue 的依赖图（`issue_dependencies` 表的唯一读写处，有向无环）：上游与下游、还在等的上游、整体替换上游时的校验（存在、
//! 不重复、不成环），以及一批新 issue 之间按 key 的引用。

use std::collections::{BTreeMap, BTreeSet};

use rusqlite::{Connection, params, params_from_iter};
use serde::{Deserialize, Serialize};

use crate::model::Status;
use muliao_platform::db::{Tx, placeholders};
use muliao_platform::errors::{Result, invalid};
use muliao_platform::ids::IssueId;

/// 依赖关系里的另一个 issue（上游或下游）与它现在的状态。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Dependency {
    pub id: IssueId,
    pub status: Status,
}

/// issue 在依赖图里的位置：它依赖的上游、依赖它的下游（都按编号排序）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Links {
    pub depends_on: Vec<Dependency>,
    pub blocks: Vec<Dependency>,
}

impl Links {
    /// 还没完成的上游：它们都完成之前，这个 issue 还轮不到。
    pub fn waiting_on(&self) -> Vec<IssueId> {
        self.depends_on.iter().filter(|d| !d.status.finished()).map(|d| d.id).collect()
    }
}

/// 让 issue_id 依赖 depends_on 会不会成环：会则返回环上的路径（issue_id 依赖 ... 依赖 issue_id），否则 None。edges 是其余的
/// 依赖边（issue, 它依赖的上游），issue_id 自己现有的上游不算（它们正被整体替换）。
pub(crate) fn cycle_through<K: Ord + Copy>(issue_id: K, depends_on: &[K], edges: &[(K, K)]) -> Option<Vec<K>> {
    let mut upstream: BTreeMap<K, Vec<K>> = BTreeMap::new();
    for &(down, up) in edges {
        if down != issue_id {
            upstream.entry(down).or_default().push(up);
        }
    }
    let mut seen = BTreeSet::new();
    let mut stack: Vec<(K, Vec<K>)> = depends_on.iter().map(|&d| (d, vec![issue_id, d])).collect();
    while let Some((node, path)) = stack.pop() {
        if node == issue_id {
            return Some(path);
        }
        if !seen.insert(node) {
            continue;
        }
        for &u in upstream.get(&node).into_iter().flatten() {
            let mut next = path.clone();
            next.push(u);
            stack.push((u, next));
        }
    }
    None
}

// 一批新 issue 里一项的上游：已有 issue 的编号，或同批另一项的 key（工具参数的 schema 用到它，只写 `//` 注释）
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(untagged)]
pub enum Upstream {
    Issue(IssueId),
    Key(String),
}

/// 一批里第 n 项在错误信息里的名字。
pub(crate) fn batch_label(n: usize, key: Option<&str>) -> String {
    match key {
        None => format!("issues[{n}]"),
        Some(k) => format!("issues[{n}] (key {k:?})"),
    }
}

/// 一批新 issue（每项是 (key, depends_on)）之间的 key 引用（不查库）：key 唯一、引用的 key 都在本批、key 之间不成环。环在这里
/// 按 key 报路径，而不是报会被回滚的编号；其余（编号存在、不重复、涉及已有编号的环）由 [`set_depends_on_in`] 在事务里查。
pub(crate) fn check_batch(items: &[(Option<&str>, &[Upstream])]) -> Result<()> {
    let mut index: BTreeMap<&str, usize> = BTreeMap::new();
    for (n, (key, _)) in items.iter().enumerate() {
        let Some(key) = key else { continue };
        if key.trim().is_empty() {
            return Err(invalid(format!("{}: key must not be empty", batch_label(n, Some(key)))));
        }
        if let Some(first) = index.insert(key, n) {
            return Err(invalid(format!("key {key:?} is used by issues[{first}] and issues[{n}]")));
        }
    }
    let mut edges = Vec::new();
    for (n, (key, depends_on)) in items.iter().enumerate() {
        for d in depends_on.iter() {
            if let Upstream::Key(k) = d {
                let Some(&up) = index.get(k.as_str()) else {
                    return Err(invalid(format!(
                        "{}: depends_on key {k:?} is not a key of this batch",
                        batch_label(n, *key)
                    )));
                };
                edges.push((n, up));
            }
        }
    }
    for n in 0..items.len() {
        let ups: Vec<usize> = edges.iter().filter(|(d, _)| *d == n).map(|(_, u)| *u).collect();
        if let Some(cycle) = cycle_through(n, &ups, &edges) {
            let path: Vec<String> = cycle.iter().map(|&i| format!("{:?}", items[i].0.unwrap_or(""))).collect();
            return Err(invalid(format!(
                "dependencies must not form a cycle: {} (each depends on the next)",
                path.join(" -> ")
            )));
        }
    }
    Ok(())
}

/// 一批 issue 的上游与下游（带对方的当前状态）：两次查询，与 issue 数无关。
pub fn links_in(c: &Connection, issue_ids: &[IssueId]) -> Result<BTreeMap<IssueId, Links>> {
    let mut links: BTreeMap<IssueId, Links> = issue_ids.iter().map(|&i| (i, Links::default())).collect();
    if issue_ids.is_empty() {
        return Ok(links);
    }
    let m = placeholders(issue_ids.len());
    let sides = [
        (
            format!(
                "SELECT d.issue_id, i.id, i.status FROM issue_dependencies d JOIN issues i ON i.id = d.depends_on_id WHERE d.issue_id IN ({m}) ORDER BY i.id"
            ),
            true,
        ),
        (
            format!(
                "SELECT d.depends_on_id, i.id, i.status FROM issue_dependencies d JOIN issues i ON i.id = d.issue_id WHERE d.depends_on_id IN ({m}) ORDER BY i.id"
            ),
            false,
        ),
    ];
    for (sql, upstream) in sides {
        let mut stmt = c.prepare(&sql)?;
        let rows = stmt
            .query_map(params_from_iter(issue_ids), |r| {
                Ok((r.get::<_, IssueId>(0)?, r.get::<_, IssueId>(1)?, r.get::<_, Status>(2)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        for (at, other, status) in rows {
            let entry = links.get_mut(&at).expect("queried for these issues");
            let side = if upstream { &mut entry.depends_on } else { &mut entry.blocks };
            side.push(Dependency { id: other, status });
        }
    }
    Ok(links)
}

pub fn links_of_in(c: &Connection, issue_id: IssueId) -> Result<Links> {
    Ok(links_in(c, &[issue_id])?.remove(&issue_id).unwrap_or_default())
}

/// 依赖这个 issue 的下游（按编号排序）。
pub(crate) fn downstream_in(c: &Connection, issue_id: IssueId) -> Result<Vec<IssueId>> {
    Ok(links_of_in(c, issue_id)?.blocks.into_iter().map(|d| d.id).collect())
}

/// 把 issue 的上游整体换成 depends_on：上游必须存在、不能重复、不能成环。返回（原来的, 现在的），没变为 None。
pub(crate) fn set_depends_on_in(
    c: &Tx<'_>,
    issue_id: IssueId,
    depends_on: &[IssueId],
) -> Result<Option<(Vec<IssueId>, Vec<IssueId>)>> {
    let wanted: Vec<IssueId> = depends_on.iter().copied().collect::<BTreeSet<_>>().into_iter().collect();
    if wanted.len() != depends_on.len() {
        return Err(invalid(format!("depends_on lists an issue more than once: {depends_on:?}")));
    }
    let existing: BTreeSet<IssueId> = if wanted.is_empty() {
        BTreeSet::new()
    } else {
        let mut stmt = c.prepare(&format!("SELECT id FROM issues WHERE id IN ({})", placeholders(wanted.len())))?;
        stmt.query_map(params_from_iter(&wanted), |r| r.get(0))?.collect::<rusqlite::Result<_>>()?
    };
    let missing: Vec<String> = wanted.iter().filter(|u| !existing.contains(u)).map(|u| format!("#{u}")).collect();
    if !missing.is_empty() {
        return Err(invalid(format!("issue #{issue_id} cannot depend on {}: not found", missing.join(", "))));
    }
    let edges: Vec<(IssueId, IssueId)> = {
        let mut stmt = c.prepare("SELECT issue_id, depends_on_id FROM issue_dependencies")?;
        stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?
    };
    let mut current: Vec<IssueId> = edges.iter().filter(|(d, _)| *d == issue_id).map(|(_, u)| *u).collect();
    current.sort();
    if current == wanted {
        return Ok(None);
    }
    if let Some(cycle) = cycle_through(issue_id, &wanted, &edges) {
        let path: Vec<String> = cycle.iter().map(|i| format!("#{i}")).collect();
        return Err(invalid(format!(
            "dependencies must not form a cycle: {} (each depends on the next)",
            path.join(" -> ")
        )));
    }
    c.execute("DELETE FROM issue_dependencies WHERE issue_id = ?1", [issue_id])?;
    for up in &wanted {
        c.execute("INSERT INTO issue_dependencies (issue_id, depends_on_id) VALUES (?1, ?2)", params![issue_id, up])?;
    }
    Ok(Some((current, wanted)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cycles_are_found_with_their_path() {
        // 2 依赖 1，3 依赖 2；让 1 依赖 3 成环
        let edges = [(2, 1), (3, 2)];
        assert_eq!(cycle_through(1, &[3], &edges), Some(vec![1, 3, 2, 1]));
        assert_eq!(cycle_through(4, &[3], &edges), None);
        // 自己依赖自己
        assert_eq!(cycle_through(5, &[5], &edges), Some(vec![5, 5]));
        // issue 自己现有的上游不算（正被替换）
        assert_eq!(cycle_through(2, &[], &edges), None);
    }

    #[test]
    fn a_batch_checks_its_keys() {
        let none: &[Upstream] = &[];
        let on_a = [Upstream::Key("a".into())];
        let on_b = [Upstream::Key("b".into())];
        let on_x = [Upstream::Key("x".into())];
        assert!(check_batch(&[(Some("a"), none), (Some("b"), &on_a), (None, &on_b)]).is_ok());
        let dup = check_batch(&[(Some("a"), none), (Some("a"), none)]).unwrap_err();
        assert!(dup.to_string().contains("used by issues[0] and issues[1]"), "{dup}");
        let missing = check_batch(&[(Some("a"), &on_x)]).unwrap_err();
        assert!(missing.to_string().contains("not a key of this batch"), "{missing}");
        let cycle = check_batch(&[(Some("a"), &on_b), (Some("b"), &on_a)]).unwrap_err();
        assert!(cycle.to_string().contains(r#""a" -> "b" -> "a""#), "{cycle}");
        assert!(check_batch(&[(Some(" "), none)]).is_err());
    }
}
