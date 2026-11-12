//! 与 Python 版对照：同一棵资源目录树、同一份 agents 表与工具表，读出的问题、agent、角色、规则、每个 agent 能用的与能写的
//! 应完全相同。手动跑（要一份真实树的副本、库与工具表）：
//!
//! ```text
//! MULIAO_COMPARE_ROOT=<树> MULIAO_COMPARE_DB=<库> MULIAO_COMPARE_TOOLS=<工具表 JSON> MULIAO_COMPARE_OUT=<输出> \
//!   cargo test -p muliao-assets --test compare_python -- --ignored
//! ```
//! 输出与 Python 版同形状的 JSON，再用 diff 比对（问题只比路径与报给谁：原因的措辞两版引号写法不同）。

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::Arc;

use muliao_assets::access::{Actor, grants_for, reach_of, write_scope};
use muliao_assets::kinds::{Content, StoredKind};
use muliao_assets::rules::{ToolAccess, ToolSpec, ToolTable};
use muliao_assets::tree::{Owner, load_tree};
use serde_json::{Value, json};

fn rel(root: &Path, p: &Path) -> String {
    match p.strip_prefix(root) {
        Ok(r) if r.as_os_str().is_empty() => ".".into(),
        Ok(r) => r.display().to_string(),
        Err(_) => p.display().to_string(),
    }
}

fn owner(o: &Owner) -> Value {
    o.agent().map_or(Value::Null, |a| json!(a))
}

#[test]
#[ignore = "needs a copy of a real asset tree; see the module docs"]
fn same_as_python() {
    let var = |k: &str| std::env::var(k).unwrap_or_else(|_| panic!("set {k}"));
    let root = std::path::PathBuf::from(var("MULIAO_COMPARE_ROOT"));
    let tools: Vec<Value> =
        serde_json::from_str(&std::fs::read_to_string(var("MULIAO_COMPARE_TOOLS")).unwrap()).unwrap();
    let tools = Arc::new(ToolTable::of(
        tools
            .iter()
            .map(|t| ToolSpec {
                name: t["name"].as_str().unwrap().into(),
                access: if t["access"] == "read" { ToolAccess::Read } else { ToolAccess::Write },
                admin: t["admin"].as_bool().unwrap(),
            })
            .collect(),
    ));
    let conn =
        rusqlite::Connection::open_with_flags(var("MULIAO_COMPARE_DB"), rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    let mut known = BTreeMap::new();
    let mut deleting = BTreeSet::new();
    let mut stmt = conn.prepare("SELECT id, name, deleting_by FROM agents").unwrap();
    for row in stmt
        .query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, Option<String>>(2)?)))
        .unwrap()
    {
        let (id, name, by) = row.unwrap();
        if by.is_some() {
            deleting.insert(id.clone());
        }
        known.insert(id, name);
    }
    let s = load_tree(&root, &known, &deleting, 4000, None, tools);

    let mut problems: Vec<Value> = s.problems.iter().map(|p| json!([rel(&root, &p.path), p.agent])).collect();
    problems.sort_by_key(|v| v.to_string());
    let mut reasons: Vec<Value> = s.problems.iter().map(|p| json!([rel(&root, &p.path), p.reason])).collect();
    reasons.sort_by_key(|v| v.to_string());
    let roles: BTreeMap<&String, Value> = s
        .roles
        .iter()
        .map(|(n, r)| (n, json!({"description": r.description, "allow": r.rules.allow, "deny": r.rules.deny, "fixed": r.fixed.allow})))
        .collect();
    let role_order: Vec<&String> = s.roles.keys().collect();
    let rules: BTreeMap<&String, Value> = s
        .agents
        .keys()
        .map(|a| {
            (a, {
                let r = s.rules_of(a);
                json!([r.roles, r.allow, r.deny])
            })
        })
        .collect();
    let mut reach = BTreeMap::new();
    let mut scopes = BTreeMap::new();
    for a in s.agents.keys() {
        let r = reach_of(&s, &grants_for(&s, &Actor::Agent(a.clone())));
        reach.insert(a, json!({"tools": r.tool_names(), "assets": r.assets}));
        let w = write_scope(&s, a);
        let paths = |v: &[std::path::PathBuf]| v.iter().map(|p| rel(&root, p)).collect::<Vec<_>>();
        scopes.insert(a, json!({"assets": paths(&w.assets), "withheld": paths(&w.withheld), "granted": w.granted, "revoked": w.revoked, "admin": w.admin}));
    }
    let mut assets: Vec<Value> = StoredKind::ALL
        .into_iter()
        .flat_map(|k| s.of_kind(k))
        .map(|a| {
            let detail = match &a.content {
                Content::Memory(m) => json!(m.text.chars().count()),
                Content::Skill(sk) => json!(sk.approval.iter().map(|p| rel(&root, p)).collect::<Vec<_>>()),
                Content::Mcp(m) => json!([m.enabled, format!("{:?}", m.config.transport()).to_lowercase()]),
                Content::Secret(sec) => json!(sec.allow_shell),
            };
            json!([a.kind.as_str(), a.name, owner(&a.owner), a.description(), rel(&root, &a.path), detail])
        })
        .collect();
    assets.sort_by_key(|v| v.to_string());
    let mut addressable = s.addressable();
    addressable.sort();
    let mut agents: Vec<&String> = s.agents.keys().collect();
    agents.sort();
    let out = json!({
        "problems": problems, "agents": agents, "roles": roles, "role_order": role_order, "rules": rules,
        "reach": reach, "write_scope": scopes, "assets": assets, "addressable": addressable,
    });
    std::fs::write(var("MULIAO_COMPARE_OUT"), serde_json::to_string_pretty(&out).unwrap()).unwrap();
    std::fs::write(var("MULIAO_COMPARE_OUT") + ".reasons", serde_json::to_string_pretty(&reasons).unwrap()).unwrap();
}
