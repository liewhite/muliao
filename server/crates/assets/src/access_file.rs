//! `platform/access.toml`（角色定义、各 agent 的角色与 allow / deny）的格式：读（[`read`] / [`parse`]）与平台自己写
//! （[`plan`] 加各项改动）都在这一处。
//!
//! ```toml
//! [roles.trading]
//! description = "交易账户"
//! allow = ["use:skill:binance-*", "use:secret:BINANCE_*"]
//!
//! [agents.3ae657ce1d2e] # 交易员
//! roles = ["trading"]
//! allow = ["write:path:~/projects/arb"]
//! deny = ["use:tool:delete_agent"]
//! ```
//!
//! 读：语法、字段、规则格式任何一处不合法，整份都不合法（readable 为假，只剩内置角色的固定部分，没有任何 agent 的规则），
//! 由读树的一方决定沿用什么（#550）——跳过写坏的一条（尤其 deny）等于放宽权限（#573）。只引用了不存在的东西（没加载的
//! agent 目录、匹配不上任何工具的规则）只收窄：记一条问题，其余照常。
//! 写之前的校验只有 [`vet`] 一个：文件工具改它与平台自己改它（[`plan`]）都调它。平台的改动在原文上改（toml_edit），人手
//! 写的注释与排版保留。
//!
//! `[agents.*]` 的节名是 agent 的 id（改名不用动这个文件），行尾注释是它的名字：平台写这一节时写上，只给人看，读时不认。

use muliao_platform::ids::AgentId;
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::path::{Path, PathBuf};

use toml_edit::{Array, DocumentMut, Item, Table, TableLike, Value};

use crate::kinds::Kind;
use crate::naming::{AgentName, is_agent_id, validate_slug};
use crate::rules::{
    ADMIN, AgentRules, BASE, Role, Roles, Rules, ToolTable, fixed_rules, fnmatch, kind_of, matches_no_tool,
};
use crate::tree::{Expect, Snapshot};
use muliao_platform::errors::{Result, conflict, invalid};

pub const ACCESS_FILE: &str = "access.toml";

/// 内置角色没写描述时的描述
fn builtin_description(name: &str) -> Option<&'static str> {
    match name {
        BASE => Some("每个 agent 都有：全部非管理工具与全局记忆，加上这里收录的 skill、MCP server、secret、记忆"),
        ADMIN => Some("全部工具（含管理 agent 与角色），并且能改任何 AI 资源"),
        _ => None,
    }
}

/// 读出的一处问题。agent_id：与它相关的 agent（报给它），平台的为 None。invalid：形式不合法，整份因它作废。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccessProblem {
    pub reason: String,
    pub agent_id: Option<AgentId>,
    pub invalid: bool,
}

/// 读出的内容：全部角色、各 agent 的规则（按 id，没写的 agent 只有 base）。readable 为假时 roles 只有内置角色的固定
/// 部分、rules 为空，problems 只有不合法的那几处。
#[derive(Debug, Clone)]
pub struct Access {
    pub roles: Roles,
    pub rules: BTreeMap<AgentId, AgentRules>,
    pub problems: Vec<AccessProblem>,
    pub readable: bool,
}

/// 读树时 access.toml 的原文：快照里的角色与规则就是从它读出的，改这个文件（[`plan`]）也从它出发，所以预览、写入与
/// 快照算的是同一份。
#[derive(Debug, Clone)]
pub enum AccessText {
    /// 合法的内容（没有文件为 None）
    Valid(Option<String>),
    /// 读不出或不合法：每一处的说明
    Invalid(String),
}

/// 读 path（没有这个文件就是空的）。agents：加载成功的 agent 的 id。
pub fn read(path: &Path, agents: &BTreeSet<AgentId>, tools: &ToolTable) -> (Access, AccessText) {
    let text = if path.is_file() {
        match muliao_platform::fs::read_text(path) {
            Ok(text) => Some(text),
            Err(e) => {
                let mut p = Parser::new(agents, tools);
                p.invalid(format!("cannot read: {e}"));
                return (p.done(), AccessText::Invalid(format!("cannot read: {e}")));
            }
        }
    } else {
        None
    };
    let access = parse(text.as_deref(), agents, tools);
    let text = if access.readable {
        AccessText::Valid(text)
    } else {
        AccessText::Invalid(access.problems.iter().map(|p| p.reason.as_str()).collect::<Vec<_>>().join("; "))
    };
    (access, text)
}

/// 文件内容（没有这个文件为 None）读成 [`Access`]。
pub fn parse(text: Option<&str>, agents: &BTreeSet<AgentId>, tools: &ToolTable) -> Access {
    Parser::new(agents, tools).parse(text)
}

struct Parser<'a> {
    agents: &'a BTreeSet<AgentId>,
    tools: &'a ToolTable,
    problems: Vec<AccessProblem>,
    roles: Roles,
    rules: BTreeMap<AgentId, AgentRules>,
}

fn builtin_roles(tools: &ToolTable) -> Roles {
    fixed_rules(tools)
        .into_iter()
        .map(|(name, fixed)| {
            let description = builtin_description(name).unwrap_or_default().to_owned();
            (name.to_owned(), Role { name: name.to_owned(), description, rules: Rules::default(), fixed })
        })
        .collect()
}

/// 一节（[roles.x] 或 [agents.x]）读出的字段
struct Entry {
    description: String,
    roles: Vec<String>,
    allow: Vec<String>,
    deny: Vec<String>,
}

impl<'a> Parser<'a> {
    fn new(agents: &'a BTreeSet<AgentId>, tools: &'a ToolTable) -> Self {
        Self { agents, tools, problems: Vec::new(), roles: builtin_roles(tools), rules: BTreeMap::new() }
    }

    fn problem(&mut self, reason: String, agent_id: Option<AgentId>) {
        self.problems.push(AccessProblem { reason, agent_id, invalid: false });
    }

    fn invalid(&mut self, reason: String) {
        self.problems.push(AccessProblem { reason, agent_id: None, invalid: true });
    }

    fn done(self) -> Access {
        if self.problems.iter().any(|p| p.invalid) {
            let problems = self.problems.into_iter().filter(|p| p.invalid).collect();
            return Access { roles: builtin_roles(self.tools), rules: BTreeMap::new(), problems, readable: false };
        }
        Access { roles: self.roles, rules: self.rules, problems: self.problems, readable: true }
    }

    fn parse(mut self, text: Option<&str>) -> Access {
        let Some(text) = text else { return self.done() };
        let data: toml::Table = match text.parse() {
            Ok(d) => d,
            Err(e) => {
                let e: toml::de::Error = e;
                self.invalid(format!("cannot parse: {}", e.message().trim()));
                return self.done();
            }
        };
        let mut extra: Vec<&String> = data.keys().filter(|k| !matches!(k.as_str(), "roles" | "agents")).collect();
        extra.sort();
        if !extra.is_empty() {
            self.invalid(format!("unknown top-level tables {extra:?} (expected roles, agents)"));
        }
        for (name, raw) in self.table(&data, "roles") {
            self.role(&name, &raw);
        }
        for (id, raw) in self.table(&data, "agents") {
            self.agent(&id, &raw);
        }
        self.done()
    }

    /// 一张表的各节，按名字排（toml::Table 本身有序）。
    fn table(&mut self, data: &toml::Table, key: &str) -> Vec<(String, toml::Value)> {
        match data.get(key) {
            None => Vec::new(),
            Some(toml::Value::Table(t)) => t.iter().map(|(k, v)| (k.clone(), v.clone())).collect(),
            Some(_) => {
                self.invalid(format!("{key} must be a table ([{key}.<name>] sections)"));
                Vec::new()
            }
        }
    }

    /// 读一节：只认 keys 里的字段，description 是字符串，其余是字符串列表；不合的整份作废。
    fn entry(&mut self, raw: &toml::Value, keys: &[&str], what: &str) -> Option<Entry> {
        let Some(table) = raw.as_table() else {
            self.invalid(format!("{what}: must be a table"));
            return None;
        };
        let mut entry = Entry { description: String::new(), roles: vec![], allow: vec![], deny: vec![] };
        let mut ok = true;
        for (k, v) in table {
            let target = match k.as_str() {
                "description" if keys.contains(&"description") => {
                    match v.as_str() {
                        Some(s) => entry.description = s.to_owned(),
                        None => {
                            self.invalid(format!("{what}: description must be a string"));
                            ok = false;
                        }
                    }
                    continue;
                }
                "roles" if keys.contains(&"roles") => &mut entry.roles,
                "allow" => &mut entry.allow,
                "deny" => &mut entry.deny,
                _ => {
                    self.invalid(format!("{what}: unknown key {k:?} (expected {keys:?})"));
                    ok = false;
                    continue;
                }
            };
            match v.as_array().and_then(|a| a.iter().map(|x| x.as_str().map(str::to_owned)).collect::<Option<_>>()) {
                Some(list) => *target = list,
                None => {
                    self.invalid(format!("{what}: {k} must be a list of strings"));
                    ok = false;
                }
            }
        }
        ok.then_some(entry)
    }

    fn role(&mut self, name: &str, raw: &toml::Value) {
        let what = format!("role {name:?}");
        let entry = self.entry(raw, &["description", "allow", "deny"], &what);
        if let Err(e) = validate_slug("role", name) {
            self.invalid(e.message);
        }
        let Some(entry) = entry else { return };
        let mut description = entry.description.trim().to_owned();
        if description.is_empty() {
            description = builtin_description(name).unwrap_or_default().to_owned();
        }
        if description.is_empty() {
            self.invalid(format!("{what}: description must not be empty"));
        }
        let rules = Rules::new(self.patterns(&entry.allow, &what, None), self.patterns(&entry.deny, &what, None));
        let fixed = self.roles.get(name).filter(|r| r.builtin()).map(|r| r.fixed.clone()).unwrap_or_default();
        self.roles.insert(name.to_owned(), Role { name: name.to_owned(), description, rules, fixed });
    }

    fn agent(&mut self, id: &str, raw: &toml::Value) {
        let what = format!("[agents.{id}]");
        let Some(entry) = self.entry(raw, &["roles", "allow", "deny"], &what) else { return };
        if !is_agent_id(id) {
            // 照旧习惯写成了名字：不报的话这一节的规则（尤其 deny）悄悄不生效
            self.invalid(format!(
                "{what}: sections under [agents] are named by agent id (12 hex characters, shown by get_agent), \
                 with the name as a comment, e.g. [agents.143cd08d29b0] # muliao"
            ));
            return;
        }
        let agent_id = Some(AgentId::new(id)).filter(|a| self.agents.contains(a));
        let allow = self.patterns(&entry.allow, &what, agent_id.clone());
        let deny = self.patterns(&entry.deny, &what, agent_id.clone());
        // 角色定义与对它的引用在同一个文件里：引用了没定义的角色是文件自相矛盾（拼错、手删了定义），跳过它会丢掉那个
        // 角色的 deny，所以整份作废；写了隐含的 base 只是多余
        let unknown: Vec<&String> = entry.roles.iter().filter(|r| !self.roles.contains_key(*r)).collect();
        if !unknown.is_empty() {
            self.invalid(format!("{what}: roles {unknown:?} are not defined in [roles]"));
        }
        let Some(agent_id) = agent_id else {
            // 那个 agent 本来就跑不了（不存在或目录没加载），这一节暂时不起作用
            self.problem(format!("{what}: no loaded agent has this id"), None);
            return;
        };
        let implicit: Vec<&String> =
            entry.roles.iter().filter(|r| self.roles.get(*r).is_some_and(|role| !role.assignable())).collect();
        if !implicit.is_empty() {
            self.problem(
                format!("{what}: roles {implicit:?} are implicit for every agent; skipped"),
                Some(agent_id.clone()),
            );
        }
        let mut kept = Vec::new();
        for r in &entry.roles {
            if self.roles.get(r).is_some_and(Role::assignable) && !kept.contains(r) {
                kept.push(r.clone());
            }
        }
        self.rules.insert(agent_id, AgentRules { roles: kept, allow, deny });
    }

    /// 形式不对的整份作废；一个工具都匹配不上的只是引用了不存在的工具（留着也什么都不匹配），记一条问题；重复的去重。
    fn patterns(&mut self, patterns: &[String], what: &str, agent_id: Option<AgentId>) -> Vec<String> {
        let mut unique: Vec<String> = Vec::new();
        for p in patterns {
            if !unique.contains(p) {
                unique.push(p.clone());
            }
        }
        for p in &unique {
            match kind_of(p) {
                Err(e) => self.invalid(format!("{what}: {e}")),
                Ok(_) if matches_no_tool(p, self.tools) => self.problem(
                    format!(
                        "{what}: rule {p:?} matches no tool; tools are listed by get_agent, e.g. 'use:tool:issue_*'"
                    ),
                    agent_id.clone(),
                ),
                Ok(_) => {}
            }
        }
        unique
    }
}

// ---- 写 ----

/// 对 access.toml 的一次改动：已校验、还没写（[`plan`]）。before 是改之前的内容（没有文件为 None）。
#[derive(Debug, Clone)]
pub struct AccessEdit {
    path: PathBuf,
    before: Option<String>,
    after: String,
}

impl AccessEdit {
    /// 原子写入。文件已不是 plan 时的内容（期间被 shell 等改过）时 Conflict，不盖掉那次改动。
    pub fn apply(&self) -> Result<()> {
        Expect::text(self.before.as_deref()).check(&self.path)?;
        muliao_platform::fs::write_atomic(&self.path, self.after.as_bytes())?;
        Ok(())
    }

    /// 把 apply 写下的改回 before（调用方后面的步骤失败时用，仍在同一次写资源目录的锁里）。文件不是 after（没 apply
    /// 过、已改回、apply 时发现被别处改过）时不动它。
    pub fn revert(&self) -> Result<()> {
        match Expect::text(Some(&self.after)).check(&self.path) {
            Ok(()) => {}
            Err(muliao_platform::errors::Error::Domain(e))
                if e.kind == muliao_platform::errors::ErrorKind::Conflict =>
            {
                return Ok(());
            }
            Err(e) => return Err(e),
        }
        match &self.before {
            None => std::fs::remove_file(&self.path)?,
            Some(text) => muliao_platform::fs::write_atomic(&self.path, text.as_bytes())?,
        }
        Ok(())
    }
}

/// access.toml 从 before（没有文件为 None）改成 after 之前的唯一校验，不过就 InvalidInput 说明每一处：
/// - after 合法（与读树同一套规则）；
/// - 新加的规则（after 里有、before 整个文件里都没有的）至少匹配 addressable（注册的内置工具与按名字认的资源的
///   `kind:名`）里的一项，拼错的在写入时报出。已有的不查：资源删掉再建时分发不丢；比整个文件而不按节，agent 改名时它
///   那一节换了名字也不算新加。不限种类的（`write:*`）与路径规则（目录可以还没建）不查。
pub fn vet(before: Option<&str>, after: &str, tools: &ToolTable, addressable: &[String]) -> Result<()> {
    let access = parse(Some(after), &BTreeSet::new(), tools);
    if !access.readable {
        return Err(invalid(access.problems.iter().map(|p| p.reason.as_str()).collect::<Vec<_>>().join("; ")));
    }
    let old: HashSet<String> = rules_in(before).into_iter().collect();
    let mut bad: Vec<String> = Vec::new();
    for p in rules_in(Some(after)) {
        if old.contains(&p) || bad.contains(&p) || matches!(kind_of(&p), Ok(None | Some(Kind::Path))) {
            continue;
        }
        let matched = addressable
            .iter()
            .any(|i| crate::rules::Action::ALL.iter().any(|a| fnmatch(&format!("{}:{i}", a.as_str()), &p)));
        if !matched {
            bad.push(p);
        }
    }
    if !bad.is_empty() {
        return Err(invalid(format!(
            "rules {bad:?} match no existing tool, skill, MCP server, secret or shared memory; check the names (tools \
             are listed by get_agent, e.g. 'use:tool:issue_*'; private memories belong to their agent and are not \
             named in rules)"
        )));
    }
    Ok(())
}

/// 手写 access.toml 时（文件工具经写入口）：after 里新出现的 `[agents.<id>]` 节（before 没有的）中，id 不是数据库里的
/// agent（known）的那些。抄错的 id 会让这一节的 deny 不生效（等于放宽权限），所以写入口拒绝；已有的节不查（agent 删除时
/// 由平台去掉它那一节）。平台自己改它（[`plan`]）只写认得的 id，不经这里。
pub fn unknown_new_agents(before: Option<&str>, after: &str, known: &BTreeSet<AgentId>) -> Vec<AgentId> {
    // 节名就是 agent 编号：在这里读入
    let sections = |text: Option<&str>| -> Vec<AgentId> {
        let Some(Ok(data)) = text.map(|t| t.parse::<toml::Table>()) else { return Vec::new() };
        let ids = data.get("agents").and_then(toml::Value::as_table).map(|t| t.keys().map(AgentId::new).collect());
        ids.unwrap_or_default()
    };
    let old = sections(before);
    sections(Some(after)).into_iter().filter(|id| !old.contains(id) && !known.contains(id)).collect()
}

/// 文件里写着的每一条规则（[roles.*] 与 [agents.*] 的 allow、deny 里的字符串），不管形式对不对；读不出时没有。
fn rules_in(text: Option<&str>) -> Vec<String> {
    let Some(Ok(data)) = text.map(|t| t.parse::<toml::Table>()) else { return Vec::new() };
    let mut out = Vec::new();
    for section in ["roles", "agents"] {
        let Some(entries) = data.get(section).and_then(toml::Value::as_table) else { continue };
        for entry in entries.values().filter_map(toml::Value::as_table) {
            for key in ["allow", "deny"] {
                if let Some(list) = entry.get(key).and_then(toml::Value::as_array) {
                    out.extend(list.iter().filter_map(|r| r.as_str().map(str::to_owned)));
                }
            }
        }
    }
    out
}

/// 平台改 access.toml 的唯一入口：从快照读到的原文出发（不存在就是空文档）、交给 change 改、按 [`vet`] 校验，返回还没写
/// 的改动。快照里的文件不合法时 Conflict，由 admin 先修好它；改完过不了 vet 时 InvalidInput。要先写别的文件的调用方（建、
/// 改名 agent）先 plan 再动手：不合法时什么都没写。界面的预览也只 plan 不 apply。
pub fn plan(snapshot: &Snapshot, change: impl FnOnce(&mut DocumentMut) -> Result<()>) -> Result<AccessEdit> {
    let path = snapshot.access_path();
    let text = match &snapshot.access_text {
        AccessText::Valid(text) => text.clone(),
        AccessText::Invalid(reasons) => {
            return Err(conflict(format!("{} is invalid ({reasons}); fix it first", path.display())));
        }
    };
    let mut doc: DocumentMut = match &text {
        Some(t) => t.parse().map_err(|e| conflict(format!("{} cannot be edited: {e}", path.display())))?,
        None => DocumentMut::new(),
    };
    change(&mut doc)?;
    let after = doc.to_string();
    vet(text.as_deref(), &after, &snapshot.tools, &snapshot.addressable())?;
    Ok(AccessEdit { path, before: text, after })
}

fn section<'d>(doc: &'d mut DocumentMut, name: &str) -> &'d mut dyn TableLike {
    doc.entry(name)
        .or_insert_with(|| {
            let mut t = Table::new();
            t.set_implicit(true);
            Item::Table(t)
        })
        .as_table_like_mut()
        .expect("a readable access.toml has tables here")
}

fn rules_table(fields: &[(&str, FieldValue<'_>)]) -> Item {
    let mut table = Table::new();
    for (key, value) in fields {
        let value: Value = match value {
            FieldValue::Text(s) => (*s).into(),
            FieldValue::List(items) => {
                let mut array: Array = items.iter().map(String::as_str).collect();
                if items.len() > 2 {
                    for v in array.iter_mut() {
                        v.decor_mut().set_prefix("\n    ");
                    }
                    array.set_trailing("\n");
                    array.set_trailing_comma(true);
                }
                array.into()
            }
        };
        table.insert(key, Item::Value(value));
    }
    Item::Table(table)
}

/// 换掉一节的内容：原来那节的位置与前后的注释（文件开头的注释也挂在第一节前面）留着。
fn replace(parent: &mut dyn TableLike, key: &str, mut item: Item) {
    if let (Some(Item::Table(old)), Item::Table(new)) = (parent.get(key), &mut item) {
        *new.decor_mut() = old.decor().clone();
        new.set_position(old.position());
    }
    parent.insert(key, item);
}

enum FieldValue<'a> {
    Text(&'a str),
    List(&'a [String]),
}

/// 界面交来的列表不能有重复（读法遇到重复只是去重，平台自己不写进去）。
fn distinct(what: &str, values: &[String]) -> Result<()> {
    let set: HashSet<&String> = values.iter().collect();
    if set.len() != values.len() {
        return Err(invalid(format!("{what} must be distinct, got {values:?}")));
    }
    Ok(())
}

pub fn set_role(doc: &mut DocumentMut, name: &str, description: &str, rules: &Rules) -> Result<()> {
    distinct("rules", &rules.allow)?;
    distinct("rules", &rules.deny)?;
    let mut fields = vec![("description", FieldValue::Text(description)), ("allow", FieldValue::List(&rules.allow))];
    if !rules.deny.is_empty() {
        fields.push(("deny", FieldValue::List(&rules.deny)));
    }
    replace(section(doc, "roles"), name, rules_table(&fields));
    Ok(())
}

pub fn delete_role(doc: &mut DocumentMut, name: &str) {
    section(doc, "roles").remove(name);
}

/// agent（id，名字写进行尾注释）的规则；全空时去掉这一节（只有 base）。roles 不能写 base（每个 agent 隐含），角色有没有
/// 定义由 vet 查。
pub fn set_agent_rules(doc: &mut DocumentMut, agent_id: &AgentId, name: &AgentName, rules: &AgentRules) -> Result<()> {
    if rules.roles.iter().any(|r| r == BASE) {
        return Err(invalid(format!("role {BASE:?} is implicit for every agent; do not list it in roles")));
    }
    distinct("roles", &rules.roles)?;
    distinct("rules", &rules.allow)?;
    distinct("rules", &rules.deny)?;
    let fields: Vec<(&str, FieldValue<'_>)> = [("roles", &rules.roles), ("allow", &rules.allow), ("deny", &rules.deny)]
        .into_iter()
        .filter(|(_, v)| !v.is_empty())
        .map(|(k, v)| (k, FieldValue::List(v)))
        .collect();
    let agents = section(doc, "agents");
    if fields.is_empty() {
        agents.remove(agent_id.as_str());
        return Ok(());
    }
    replace(agents, agent_id.as_str(), rules_table(&fields));
    if let Some(Item::Table(t)) = agents.get_mut(agent_id.as_str()) {
        t.decor_mut().set_suffix(format!(" # {name}"));
    }
    Ok(())
}

pub fn remove_agent(doc: &mut DocumentMut, agent_id: &AgentId) {
    section(doc, "agents").remove(agent_id.as_str());
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rules::{ToolAccess, ToolSpec};

    fn tools() -> ToolTable {
        ToolTable::of(vec![
            ToolSpec { name: "read_file".into(), access: ToolAccess::Read, admin: false },
            ToolSpec { name: "create_agent".into(), access: ToolAccess::Write, admin: true },
        ])
    }

    const A1: &str = "0123456789ab";

    #[test]
    fn reads_roles_and_agents() {
        let agents = BTreeSet::from([AgentId::new(A1)]);
        let text = r#"
[roles.base]
allow = ["use:skill:x"]
[roles.trading]
description = "t"
allow = ["use:tool:nope_*"]
[agents.0123456789ab] # 交易
roles = ["trading", "base", "trading"]
deny = ["use:tool:read_file"]
[agents.ffffffffffff]
allow = []
"#;
        let a = parse(Some(text), &agents, &tools());
        assert!(a.readable, "{:?}", a.problems);
        assert_eq!(a.roles.keys().collect::<Vec<_>>(), ["base", "admin", "trading"]);
        assert_eq!(a.roles["base"].description, builtin_description("base").unwrap());
        assert!(a.roles["base"].fixed.allow.contains(&"use:tool:read_file".to_owned()));
        assert!(!a.roles["base"].fixed.allow.contains(&"use:tool:create_agent".to_owned()));
        assert_eq!(a.rules[&AgentId::new(A1)].roles, ["trading"]);
        assert_eq!(a.problems.len(), 3, "{:?}", a.problems);

        for bad in [
            "x = 1",
            "[roles.Bad]\ndescription = \"d\"",
            "[roles.r]\nallow = []",
            "[roles.r]\ndescription = \"d\"\nallow = [\"use:bad\"]",
            "[agents.0123456789ab]\nroles = [\"missing\"]",
            "[agents.0123456789ab]\nextra = 1",
            // 节名写成名字（旧格式）：整份作废，写入口也拦下
            "[agents.\"交易\"]\ndeny = [\"use:tool:read_file\"]",
            "[roles.r]\ndescription = 1",
            "not toml [",
        ] {
            let a = parse(Some(bad), &agents, &tools());
            assert!(!a.readable, "{bad}");
            assert!(a.rules.is_empty());
            assert_eq!(a.roles.len(), 2);
        }
    }

    #[test]
    fn vet_checks_only_new_rules() {
        let t = tools();
        let addressable = vec!["tool:read_file".to_owned(), "skill:s".to_owned()];
        let before = "[roles.r]\ndescription = \"d\"\nallow = [\"use:skill:gone\"]\n";
        assert!(vet(Some(before), before, &t, &addressable).is_ok());
        let after = "[roles.r]\ndescription = \"d\"\nallow = [\"use:skill:gone\", \"use:skill:typo\", \"write:*\", \"write:path:/x\"]\n";
        let err = vet(Some(before), after, &t, &addressable).unwrap_err().to_string();
        assert!(err.contains("typo") && !err.contains("gone"), "{err}");
        assert!(vet(None, "[roles.r]\ndescription = \"d\"\nallow = [\"*:skill:s\"]\n", &t, &addressable).is_ok());
    }

    #[test]
    fn edits_keep_comments() {
        let mut doc: DocumentMut =
            "# 头注释\n[roles.r]\n# 角色注释\ndescription = \"d\"\nallow = []\n".parse().unwrap();
        let rules = AgentRules { roles: vec!["r".into()], allow: vec![], deny: vec![] };
        let name = |n: &str| AgentName::parse(n).unwrap();
        set_agent_rules(&mut doc, &AgentId::new(A1), &name("交易"), &rules).unwrap();
        set_role(
            &mut doc,
            "s",
            "desc",
            &Rules::new(vec!["use:skill:a".into(), "use:skill:b".into(), "use:skill:c".into()], vec![]),
        )
        .unwrap();
        // 改名：只换行尾注释
        set_agent_rules(&mut doc, &AgentId::new(A1), &name("交易员"), &rules).unwrap();
        let out = doc.to_string();
        assert!(out.starts_with("# 头注释\n[roles.r]\n# 角色注释\n"), "{out}");
        assert!(out.contains("[agents.0123456789ab] # 交易员\nroles = [\"r\"]"), "{out}");
        assert!(!out.contains("# 交易\n"), "{out}");
        assert!(
            out.contains("allow = [\n    \"use:skill:a\",\n    \"use:skill:b\",\n    \"use:skill:c\",\n]"),
            "{out}"
        );
        assert!(!out.contains("[roles]") && !out.contains("[agents]\n"), "{out}");
        let back = parse(Some(&out), &BTreeSet::from([AgentId::new(A1)]), &tools());
        assert!(back.readable, "{:?}", back.problems);
        assert_eq!(back.rules[&AgentId::new(A1)].roles, ["r"]);
        set_agent_rules(&mut doc, &AgentId::new(A1), &name("交易员"), &AgentRules::default()).unwrap();
        assert!(!doc.to_string().contains("交易员"));
        let x = name("x");
        assert!(
            set_agent_rules(
                &mut doc,
                &AgentId::new(A1),
                &x,
                &AgentRules { roles: vec!["base".into()], ..Default::default() }
            )
            .is_err()
        );
        assert!(
            set_agent_rules(
                &mut doc,
                &AgentId::new(A1),
                &x,
                &AgentRules { roles: vec!["r".into(), "r".into()], ..Default::default() }
            )
            .is_err()
        );
    }
}
