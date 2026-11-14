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
//! 读：语法、字段、规则格式任何一处不合法，整份都不合法（[`Access::Invalid`]，没有任何角色与规则），
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
use crate::naming::{AgentName, is_agent_id};
use crate::roles::{AgentRules, AssignedRole, BoundRules, RoleName, Roles};
use crate::rules::{Rule, RuleList, Rules, ToolTable, builtin_roles, texts};
use crate::tree::{Expect, Snapshot};
use muliao_platform::errors::{Result, conflict, invalid};
use serde::Deserialize;

pub const ACCESS_FILE: &str = "access.toml";

/// 读出的一处只收窄的问题（引用了不存在的东西）。agent_id：与它相关的 agent（报给它），平台的为 None。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccessProblem {
    pub reason: String,
    pub agent_id: Option<AgentId>,
}

/// 读出的结果。
#[derive(Debug, Clone)]
pub enum Access {
    /// 合法：全部角色、各 agent 的规则（按 id，没写的 agent 只有 base）、只收窄的问题，与读出它们的原文（没有文件
    /// 为 None）
    Valid {
        roles: Box<Roles>,
        rules: BTreeMap<AgentId, BoundRules>,
        problems: Vec<AccessProblem>,
        text: Option<String>,
    },
    /// 读不出或不合法，整份作废：每一处的原因
    Invalid(Vec<String>),
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
pub fn read(path: &Path, agents: &BTreeSet<AgentId>, tools: &ToolTable) -> Access {
    if !path.is_file() {
        return parse(None, agents, tools);
    }
    match muliao_platform::fs::read_text(path) {
        Ok(text) => parse(Some(&text), agents, tools),
        Err(e) => Access::Invalid(vec![format!("cannot read: {e}")]),
    }
}

/// 文件内容（没有这个文件为 None）读成 [`Access`]。
pub fn parse(text: Option<&str>, agents: &BTreeSet<AgentId>, tools: &ToolTable) -> Access {
    Parser::new(agents, tools).parse(text)
}

struct Parser<'a> {
    agents: &'a BTreeSet<AgentId>,
    tools: &'a ToolTable,
    problems: Vec<AccessProblem>,
    /// 形式不合法的各处：有一处整份就作废
    invalid: Vec<String>,
    roles: Roles,
    rules: BTreeMap<AgentId, BoundRules>,
}

/// 文件的样子：读入只经它（未知的表与键、类型不对、规则形式不对都在这里报出）。节按名字排。
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Stored {
    #[serde(default)]
    roles: BTreeMap<String, StoredRole>,
    /// 节名是 agent 的 id
    #[serde(default)]
    agents: BTreeMap<String, StoredAgent>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredRole {
    #[serde(default)]
    description: String,
    #[serde(default)]
    allow: RuleList,
    #[serde(default)]
    deny: RuleList,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredAgent {
    #[serde(default)]
    roles: Vec<String>,
    #[serde(default)]
    allow: RuleList,
    #[serde(default)]
    deny: RuleList,
}

/// 文件内容读成 [`Stored`]；不合格式时是原因。
fn stored(text: &str) -> std::result::Result<Stored, String> {
    // 带位置与那一行原文的报错（哪一节写错了）；这个文件里没有密钥
    toml::from_str(text).map_err(|e: toml::de::Error| format!("cannot parse: {}", e.to_string().trim()))
}

impl<'a> Parser<'a> {
    fn new(agents: &'a BTreeSet<AgentId>, tools: &'a ToolTable) -> Self {
        Self {
            agents,
            tools,
            problems: Vec::new(),
            invalid: Vec::new(),
            roles: builtin_roles(tools),
            rules: BTreeMap::new(),
        }
    }

    fn problem(&mut self, reason: String, agent_id: Option<AgentId>) {
        self.problems.push(AccessProblem { reason, agent_id });
    }

    fn invalid(&mut self, reason: String) {
        self.invalid.push(reason);
    }

    fn done(self, text: Option<&str>) -> Access {
        if !self.invalid.is_empty() {
            return Access::Invalid(self.invalid);
        }
        Access::Valid {
            roles: Box::new(self.roles),
            rules: self.rules,
            problems: self.problems,
            text: text.map(str::to_owned),
        }
    }

    fn parse(mut self, text: Option<&str>) -> Access {
        let Some(text) = text else { return self.done(None) };
        let data = match stored(text) {
            Ok(d) => d,
            Err(e) => {
                self.invalid(e);
                return self.done(Some(text));
            }
        };
        for (name, role) in data.roles {
            self.role(&name, role);
        }
        for (id, agent) in data.agents {
            self.agent(&id, agent);
        }
        self.done(Some(text))
    }

    fn role(&mut self, name: &str, entry: StoredRole) {
        let what = format!("role {name:?}");
        let parsed = match RoleName::parse(name) {
            Ok(n) => Some(n),
            Err(e) => {
                self.invalid(e.message);
                None
            }
        };
        let mut description = entry.description.trim().to_owned();
        if description.is_empty() {
            description = parsed.as_ref().and_then(RoleName::default_description).unwrap_or_default().to_owned();
        }
        if description.is_empty() {
            self.invalid(format!("{what}: description must not be empty"));
        }
        let rules = Rules::new(self.patterns(entry.allow, &what, None), self.patterns(entry.deny, &what, None));
        if let Some(name) = parsed {
            self.roles.define(name, description, rules);
        }
    }

    fn agent(&mut self, id: &str, entry: StoredAgent) {
        let what = format!("[agents.{id}]");
        if !is_agent_id(id) {
            // 照旧习惯写成了名字：不报的话这一节的规则（尤其 deny）悄悄不生效
            self.invalid(format!(
                "{what}: sections under [agents] are named by agent id (12 hex characters, shown by get_agent), \
                 with the name as a comment, e.g. [agents.143cd08d29b0] # muliao"
            ));
            return;
        }
        let agent_id = Some(AgentId::new(id)).filter(|a| self.agents.contains(a));
        let allow = self.patterns(entry.allow, &what, agent_id.clone());
        let deny = self.patterns(entry.deny, &what, agent_id.clone());
        let mut roles = Vec::new();
        let mut implicit = Vec::new();
        for raw in &entry.roles {
            match RoleName::parse(raw) {
                Ok(name) => match name.assigned() {
                    Some(assigned) => roles.push(assigned),
                    None => implicit.push(name),
                },
                Err(e) => self.invalid(format!("{what}: {}", e.message)),
            }
        }
        // 角色定义与对它的引用在同一个文件里：引用了没定义的角色是文件自相矛盾（拼错、手删了定义），跳过它会丢掉那个
        // 角色的 deny，所以整份作废；写了隐含的 base、写重复的只是多余
        let bound = match self.roles.bind(&AgentRules { roles, allow, deny }) {
            Ok(bound) => Some(bound),
            Err(e) => {
                self.invalid(format!("{what}: {} in [roles]", e.message));
                None
            }
        };
        let Some(agent_id) = agent_id else {
            // 那个 agent 本来就跑不了（不存在或目录没加载），这一节暂时不起作用
            self.problem(format!("{what}: no loaded agent has this id"), None);
            return;
        };
        if !implicit.is_empty() {
            self.problem(
                format!("{what}: roles {implicit:?} are implicit for every agent; skipped"),
                Some(agent_id.clone()),
            );
        }
        if let Some(bound) = bound {
            self.rules.insert(agent_id, bound);
        }
    }

    /// 一个工具都匹配不上的只是引用了不存在的工具（留着也什么都不匹配），记一条问题；重复的去重。
    fn patterns(&mut self, rules: RuleList, what: &str, agent_id: Option<AgentId>) -> Vec<Rule> {
        let mut unique: Vec<Rule> = Vec::new();
        for rule in rules.0 {
            if unique.contains(&rule) {
                continue;
            }
            if rule.matches_no_tool(self.tools) {
                self.problem(
                    format!(
                        "{what}: rule {:?} matches no tool; tools are listed by get_agent, e.g. 'use:tool:issue_*'",
                        rule.to_string()
                    ),
                    agent_id.clone(),
                );
            }
            unique.push(rule);
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
/// - 新加的规则（after 里有、before 整个文件里都没有的）至少匹配 addressable（注册的内置工具与按名字认的资源）
///   里的一项，拼错的在写入时报出。已有的不查：资源删掉再建时分发不丢；比整个文件而不按节，agent 改名时它
///   那一节换了名字也不算新加。不限种类的（`write:*`）与路径规则（目录可以还没建）不查（[`Rule::matches_nothing`]）。
pub fn vet(before: Option<&str>, after: &str, tools: &ToolTable, addressable: &[(Kind, String)]) -> Result<()> {
    if let Access::Invalid(reasons) = parse(Some(after), &BTreeSet::new(), tools) {
        return Err(invalid(reasons.join("; ")));
    }
    let old: HashSet<Rule> = rules_in(before).into_iter().collect();
    let mut bad: Vec<Rule> = Vec::new();
    for r in rules_in(Some(after)) {
        if !old.contains(&r) && !bad.contains(&r) && r.matches_nothing(addressable) {
            bad.push(r);
        }
    }
    if !bad.is_empty() {
        let bad = texts(&bad);
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
    let sections =
        |text: Option<&str>| -> Vec<AgentId> { stored_or_empty(text).agents.into_keys().map(AgentId::new).collect() };
    let old = sections(before);
    sections(Some(after)).into_iter().filter(|id| !old.contains(id) && !known.contains(id)).collect()
}

/// 读得出的文件；没有文件或读不出时是空的（给只比较前后的 [`vet`] 与 [`unknown_new_agents`]：读不出的 after 已由 vet 拒绝，
/// 读不出的 before 当作什么都没有，只会多查）。
fn stored_or_empty(text: Option<&str>) -> Stored {
    text.and_then(|t| stored(t).ok()).unwrap_or(Stored { roles: BTreeMap::new(), agents: BTreeMap::new() })
}

/// 文件里写着的每一条规则（[roles.*] 与 [agents.*] 的 allow、deny），不管所在的节对不对。
fn rules_in(text: Option<&str>) -> Vec<Rule> {
    let data = stored_or_empty(text);
    let roles = data.roles.into_values().flat_map(|r| [r.allow.0, r.deny.0]);
    let agents = data.agents.into_values().flat_map(|a| [a.allow.0, a.deny.0]);
    roles.chain(agents).flatten().collect()
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
    List(Vec<String>),
}

/// 界面交来的列表不能有重复（读法遇到重复只是去重，平台自己不写进去）。
fn distinct<T: Eq + std::hash::Hash + std::fmt::Display>(what: &str, values: &[T]) -> Result<()> {
    let set: HashSet<&T> = values.iter().collect();
    if set.len() != values.len() {
        let shown: Vec<String> = values.iter().map(T::to_string).collect();
        return Err(invalid(format!("{what} must be distinct, got {shown:?}")));
    }
    Ok(())
}

pub fn set_role(doc: &mut DocumentMut, name: &RoleName, description: &str, rules: &Rules) -> Result<()> {
    distinct("rules", &rules.allow)?;
    distinct("rules", &rules.deny)?;
    let mut fields =
        vec![("description", FieldValue::Text(description)), ("allow", FieldValue::List(texts(&rules.allow)))];
    if !rules.deny.is_empty() {
        fields.push(("deny", FieldValue::List(texts(&rules.deny))));
    }
    replace(section(doc, "roles"), name.as_str(), rules_table(&fields));
    Ok(())
}

pub fn delete_role(doc: &mut DocumentMut, name: &RoleName) {
    section(doc, "roles").remove(name.as_str());
}

/// agent（id，名字写进行尾注释）的规则；全空时去掉这一节（只有 base）。roles 不能写 base（每个 agent 隐含），角色有没有
/// 定义由 vet 查。
pub fn set_agent_rules(doc: &mut DocumentMut, agent_id: &AgentId, name: &AgentName, rules: &AgentRules) -> Result<()> {
    distinct("roles", &rules.roles)?;
    distinct("rules", &rules.allow)?;
    distinct("rules", &rules.deny)?;
    let fields: Vec<(&str, FieldValue<'_>)> = [
        ("roles", rules.roles.iter().map(AssignedRole::to_string).collect()),
        ("allow", texts(&rules.allow)),
        ("deny", texts(&rules.deny)),
    ]
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

    fn rule(s: &str) -> Rule {
        Rule::parse(s).unwrap()
    }

    fn role(s: &str) -> AssignedRole {
        AssignedRole::parse(s).unwrap()
    }

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
        let Access::Valid { roles, rules, problems, .. } = parse(Some(text), &agents, &tools()) else {
            panic!("valid")
        };
        assert_eq!(roles.values().map(|r| r.name.as_str()).collect::<Vec<_>>(), ["base", "admin", "trading"]);
        let base = roles.base();
        assert_eq!(base.description, RoleName::Base.default_description().unwrap());
        assert!(base.fixed.allow.contains(&rule("use:tool:read_file")));
        assert!(!base.fixed.allow.contains(&rule("use:tool:create_agent")));
        assert_eq!(rules[&AgentId::new(A1)].written().roles, [role("trading")]);
        assert_eq!(problems.len(), 3, "{problems:?}");

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
            assert!(matches!(parse(Some(bad), &agents, &tools()), Access::Invalid(_)), "{bad}");
        }
    }

    #[test]
    fn vet_checks_only_new_rules() {
        let t = tools();
        let addressable = vec![(Kind::Tool, "read_file".to_owned()), (Kind::Skill, "s".to_owned())];
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
        let rules = AgentRules { roles: vec![role("r")], allow: vec![], deny: vec![] };
        let name = |n: &str| AgentName::parse(n).unwrap();
        set_agent_rules(&mut doc, &AgentId::new(A1), &name("交易"), &rules).unwrap();
        set_role(
            &mut doc,
            &RoleName::parse("s").unwrap(),
            "desc",
            &Rules::new(vec![rule("use:skill:a"), rule("use:skill:b"), rule("use:skill:c")], vec![]),
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
        let Access::Valid { rules: back, .. } = parse(Some(&out), &BTreeSet::from([AgentId::new(A1)]), &tools()) else {
            panic!("valid")
        };
        assert_eq!(back[&AgentId::new(A1)].written().roles, [role("r")]);
        set_agent_rules(&mut doc, &AgentId::new(A1), &name("交易员"), &AgentRules::default()).unwrap();
        assert!(!doc.to_string().contains("交易员"));
        let x = name("x");
        assert!(
            set_agent_rules(
                &mut doc,
                &AgentId::new(A1),
                &x,
                &AgentRules { roles: vec![role("r"), role("r")], ..Default::default() }
            )
            .is_err()
        );
    }
}
