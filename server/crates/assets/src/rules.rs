//! 分发规则（AI 资产的 RBAC）：`action:kind:name` 通配符规则、内置角色与内置工具表，在这里唯一登记。用途是防误操作、
//! 减少无关工具与资产占用上下文，不是安全隔离。
//!
//! - 权限 = 动作 + 资产：`use:skill:relay-host`、`write:memory:trading`。动作只有 use（skill 进索引、MCP 连上、secret
//!   能解析、记忆放进指令、工具开放）与 write（改、删）；tool 只有 use。
//! - 规则：allow / deny 两组 `action:kind:glob`，整串 fnmatch（`*` 匹配任意字符，含 `:`），deny 优先。[`Rules::allows`]
//!   是全系统唯一的判断；一组规则「收录」一个资产即它放行 use。
//! - agent 的规则 = 内置角色 base（每个 agent 隐含）+ 它的各个角色 + 它自己的 allow / deny（各自取并集）。
//! - admin 就是规则放行不限种类的写（`write:*`，[`Rules::allows_every`]）：内置角色 admin 固定带它。
//! - 本机可写目录（`write:path:<目录>`）：字面目录、不带通配符、不经 fnmatch，`write:*` 不含路径（[`Rules::paths`]）。
//! - 内置角色的规则 = 代码里固定的部分（[`fixed_rules`]，按工具表算，新加的工具自动归入）∪ access.toml 里可编辑的部分。

use std::sync::OnceLock;

use indexmap::IndexMap;

use crate::kinds::Kind;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Use,
    Write,
}

impl Action {
    pub const ALL: [Action; 2] = [Action::Use, Action::Write];

    pub fn as_str(self) -> &'static str {
        match self {
            Action::Use => "use",
            Action::Write => "write",
        }
    }
}

/// 内置工具读还是写（工具自己声明，定义在平台层的工具契约里）
pub use muliao_platform::tooling::ToolAccess;

/// 一个内置工具在分发里的样子：名字、读 / 写、是否管理类（base 不含，只经 admin 角色或显式规则给出）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolSpec {
    pub name: String,
    pub access: ToolAccess,
    pub admin: bool,
}

/// 当前配置下注册的内置工具，按注册顺序。工具集要用资源目录与分发构造，资源目录又要它：组合根先建它、装好工具集后
/// [`ToolTable::bind`] 一次；bind 之前用到是组合根的 bug，当场 panic。
#[derive(Debug, Default)]
pub struct ToolTable {
    specs: OnceLock<Vec<ToolSpec>>,
}

impl ToolTable {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn of(specs: Vec<ToolSpec>) -> Self {
        let table = Self::new();
        table.bind(specs);
        table
    }

    pub fn bind(&self, specs: Vec<ToolSpec>) {
        if self.specs.set(specs).is_err() {
            panic!("the tool table is already bound");
        }
    }

    pub fn specs(&self) -> &[ToolSpec] {
        self.specs.get().expect("the tool table is used before the composition root bound it")
    }

    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.specs().iter().map(|s| s.name.as_str())
    }
}

/// 一个资产的名字：`kind:name`。
pub fn pattern_of(kind: Kind, name: &str) -> String {
    format!("{kind}:{name}")
}

/// 一个权限：`action:kind:name`，规则按整串 fnmatch 匹配它。
pub fn permission_of(action: Action, kind: Kind, name: &str) -> String {
    format!("{}:{kind}:{name}", action.as_str())
}

/// Python 的 fnmatchcase：`*` 任意串、`?` 任一字符、`[seq]` / `[!seq]` 字符集（含 `a-z` 区间）；`[` 没闭合时按字面。
pub fn fnmatch(text: &str, pattern: &str) -> bool {
    let t: Vec<char> = text.chars().collect();
    let p: Vec<char> = pattern.chars().collect();
    let (mut ti, mut pi) = (0, 0);
    // 最近一个 `*` 的位置与它此刻吞到的位置：失配时让它多吞一个字符
    let mut star: Option<(usize, usize)> = None;
    while ti < t.len() {
        let step = match p.get(pi) {
            Some('*') => {
                star = Some((pi, ti));
                pi += 1;
                continue;
            }
            Some('?') => Some(1),
            Some('[') => match class(&p[pi..], t[ti]) {
                Some((true, len)) => Some(len),
                Some((false, _)) => None,
                None => (t[ti] == '[').then_some(1),
            },
            Some(c) => (*c == t[ti]).then_some(1),
            None => None,
        };
        match step {
            Some(len) => {
                pi += len;
                ti += 1;
            }
            None => match star {
                Some((sp, st)) => {
                    pi = sp + 1;
                    ti = st + 1;
                    star = Some((sp, st + 1));
                }
                None => return false,
            },
        }
    }
    p[pi..].iter().all(|c| *c == '*')
}

/// `[...]` 字符集是否匹配 c：(匹配, 字符集在模式里占几个字符)；没闭合为 None。
fn class(p: &[char], c: char) -> Option<(bool, usize)> {
    let mut i = 1;
    let negate = p.get(i) == Some(&'!');
    if negate {
        i += 1;
    }
    // 紧跟在 `[` 或 `[!` 后的 `]` 是字面字符
    let first = i;
    let mut matched = false;
    loop {
        let ch = *p.get(i)?;
        if ch == ']' && i > first {
            return Some((matched != negate, i + 1));
        }
        if p.get(i + 1) == Some(&'-') && p.get(i + 2).is_some_and(|e| *e != ']') {
            let end = p[i + 2];
            if ch <= c && c <= end {
                matched = true;
            }
            i += 3;
        } else {
            if ch == c {
                matched = true;
            }
            i += 1;
        }
    }
}

const ACTIONS: [&str; 3] = ["use", "write", "*"];

/// 规则针对的种类；`write:*` 这类不限种类的为 None。不是 `action:kind:glob`（action 为 use、write 或 *，kind 为
/// [`Kind`] 之一，glob 非空）或 `action:*` 形式时报错（原因）；tool 只有 use；path 只有 `write:path:<字面目录>`。
pub fn kind_of(pattern: &str) -> Result<Option<Kind>, String> {
    let (action, rest) = pattern.split_once(':').unwrap_or((pattern, ""));
    let (kind, glob) = rest.split_once(':').map_or((rest, None), |(k, g)| (k, Some(g)));
    let kind = Kind::parse(kind);
    let well_formed =
        ACTIONS.contains(&action) && (rest == "*" || (kind.is_some() && glob.is_some_and(|g| !g.is_empty())));
    if !well_formed {
        return Err(format!(
            "rule {pattern:?} must look like 'action:kind:name' with action one of {ACTIONS:?} and kind one of {:?} \
             (fnmatch wildcards allowed in the name), or 'action:*'; e.g. 'use:tool:issue_*', 'use:skill:relay-host', \
             '*:memory:trading', 'write:*'",
            Kind::ALL.map(Kind::as_str)
        ));
    }
    if rest == "*" {
        return Ok(None);
    }
    let glob = glob.unwrap_or_default();
    if kind == Some(Kind::Tool) && action == "write" {
        return Err(format!("rule {pattern:?}: built-in tools can only be used ('use:tool:...')"));
    }
    if kind == Some(Kind::Path)
        && (action != "write" || !(glob.starts_with('/') || glob.starts_with("~/")) || glob.contains(['*', '?', '[']))
    {
        return Err(format!(
            "rule {pattern:?}: a writable path rule is 'write:path:<directory>' with an absolute or '~/' path and no \
             wildcards, e.g. 'write:path:~/projects/arb'; it lets the agent write that directory and everything under it"
        ));
    }
    Ok(kind)
}

/// 一条 tool 规则匹配不上 tools 里的任何工具（拼错、工具改了名，或这次配置没注册它）。
pub fn matches_no_tool(pattern: &str, tools: &ToolTable) -> bool {
    matches!(kind_of(pattern), Ok(Some(Kind::Tool)))
        && !tools.names().any(|t| fnmatch(&permission_of(Action::Use, Kind::Tool, t), pattern))
}

/// 一组 allow / deny 的 `action:kind:glob`；deny 优先。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Rules {
    pub allow: Vec<String>,
    pub deny: Vec<String>,
}

fn push_unique(into: &mut Vec<String>, from: &[String]) {
    for p in from {
        if !into.contains(p) {
            into.push(p.clone());
        }
    }
}

impl Rules {
    pub fn new(allow: Vec<String>, deny: Vec<String>) -> Self {
        Self { allow, deny }
    }

    pub fn union<'a>(rules: impl IntoIterator<Item = &'a Rules>) -> Rules {
        let mut out = Rules::default();
        for r in rules {
            push_unique(&mut out.allow, &r.allow);
            push_unique(&mut out.deny, &r.deny);
        }
        out
    }

    /// 这组规则放行这个权限：allow 匹配、deny 没拦。全系统唯一的判断。name 为 None 是规则不按名字指的资产（agent 的
    /// 私有记忆）：只有不限种类的规则管得到它（[`Rules::allows_every`]）。
    pub fn allows(&self, action: Action, kind: Kind, name: Option<&str>) -> bool {
        permits(&self.allow, &self.deny, action, kind, name)
    }

    /// 不限种类的规则（`write:*`、`*:*`）放行这个动作、也没有不限种类的 deny 拦它：对一切资产都能做。写上就是 admin。
    pub fn allows_every(&self, action: Action) -> bool {
        permits_every(&self.allow, &self.deny, action)
    }

    /// 收录：放行 use。
    pub fn includes(&self, kind: Kind, name: Option<&str>) -> bool {
        self.allows(Action::Use, kind, name)
    }

    /// `write:path:<目录>` 规则里的目录：(allow 的, deny 的)，按规则顺序。
    pub fn paths(&self) -> (Vec<String>, Vec<String>) {
        const PREFIX: &str = "write:path:";
        let pick = |v: &[String]| v.iter().filter_map(|p| p.strip_prefix(PREFIX).map(str::to_owned)).collect();
        (pick(&self.allow), pick(&self.deny))
    }

    /// available（内置工具的名字）里放行的，按 available 的顺序。
    pub fn tools<'a>(&self, available: impl IntoIterator<Item = &'a str>) -> Vec<&'a str> {
        available.into_iter().filter(|n| self.includes(Kind::Tool, Some(n))).collect()
    }
}

/// [`Rules::allows`] 的判断本身：allow 与 deny 可以来自不同的规则（某一来源的 allow 对最终的 deny，[`crate::access::Grants`]）。
pub fn permits(allow: &[String], deny: &[String], action: Action, kind: Kind, name: Option<&str>) -> bool {
    let Some(name) = name else { return permits_every(allow, deny, action) };
    let permission = permission_of(action, kind, name);
    allow.iter().any(|p| fnmatch(&permission, p)) && !deny.iter().any(|p| fnmatch(&permission, p))
}

fn permits_every(allow: &[String], deny: &[String], action: Action) -> bool {
    let every = [format!("{}:*", action.as_str()), "*:*".to_owned()];
    allow.iter().any(|p| every.contains(p)) && !deny.iter().any(|p| every.contains(p))
}

pub const BASE: &str = "base";
pub const ADMIN: &str = "admin";
/// 内置角色，按定义顺序
pub const BUILTIN_ROLES: [&str; 2] = [BASE, ADMIN];
/// 全局记忆：所有 agent 共享的那块记忆，由 base 收录
pub const GLOBAL_MEMORY: &str = "global";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Role {
    pub name: String,
    pub description: String,
    /// 可编辑的规则（access.toml 的 [roles.<名>]）
    pub rules: Rules,
    /// 内置角色在代码里固定的规则（自建角色为空）
    pub fixed: Rules,
}

impl Role {
    pub fn builtin(&self) -> bool {
        BUILTIN_ROLES.contains(&self.name.as_str())
    }

    /// 能写进 agent 的 roles：base 每个 agent 隐含，不能写。
    pub fn assignable(&self) -> bool {
        self.name != BASE
    }

    /// roles 是一个 agent 的 roles 字段：base 每个 agent 都有（隐含），别的要写在里面。
    pub fn held_by(&self, roles: &[String]) -> bool {
        !self.assignable() || roles.contains(&self.name)
    }

    pub fn effective(&self) -> Rules {
        Rules::union([&self.fixed, &self.rules])
    }
}

/// 全部角色：内置在前（按定义顺序），自建的按名称。
pub type Roles = IndexMap<String, Role>;

/// 内置角色固定的规则（顺序同 [`BUILTIN_ROLES`]）：base 是 tools 里全部非管理工具与全局记忆（写成纯 allow：deny 优先于
/// 一切 allow，写成 deny 的话 admin 也放不开）；admin 是全部工具，并能改任何资产。
pub fn fixed_rules(tools: &ToolTable) -> [(&'static str, Rules); 2] {
    let mut base: Vec<String> =
        tools.specs().iter().filter(|t| !t.admin).map(|t| permission_of(Action::Use, Kind::Tool, &t.name)).collect();
    base.push(permission_of(Action::Use, Kind::Memory, GLOBAL_MEMORY));
    [(BASE, Rules::new(base, vec![])), (ADMIN, Rules::new(vec!["use:tool:*".into(), "write:*".into()], vec![]))]
}

/// agent 的分发规则：角色（不含每个 agent 隐含的 base）与它自己的 allow / deny。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentRules {
    pub roles: Vec<String>,
    pub allow: Vec<String>,
    pub deny: Vec<String>,
}

impl AgentRules {
    /// 它自己的 allow / deny（不含角色）。
    pub fn own(&self) -> Rules {
        Rules::new(self.allow.clone(), self.deny.clone())
    }

    /// 最终规则：各来源（[`AgentRules::sources`]）的并集。
    pub fn effective(&self, known: &Roles) -> Rules {
        let sources = self.sources(known);
        Rules::union(sources.iter().map(|(_, r)| r))
    }

    /// 最终规则的各个来源：(角色名, 它的规则)——base 在前、再按它的 roles——最后是 (None, 它自己的 allow / deny)。
    /// 不存在的角色读树时已跳过，这里再遇到是代码错误。
    pub fn sources(&self, known: &Roles) -> Vec<(Option<String>, Rules)> {
        let missing: Vec<&String> = self.roles.iter().filter(|r| !known.contains_key(*r)).collect();
        assert!(missing.is_empty(), "agent references unknown roles {missing:?}");
        std::iter::once(BASE)
            .chain(self.roles.iter().map(String::as_str))
            .map(|r| (Some(r.to_owned()), known[r].effective()))
            .chain(std::iter::once((None, self.own())))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnmatch_like_python() {
        assert!(fnmatch("use:tool:issue_list", "use:tool:issue_*"));
        assert!(fnmatch("write:memory:x", "write:*"));
        assert!(fnmatch("use:skill:a", "*:skill:?"));
        assert!(!fnmatch("use:skill:ab", "*:skill:?"));
        assert!(fnmatch("use:skill:b", "use:skill:[a-c]"));
        assert!(!fnmatch("use:skill:b", "use:skill:[!a-c]"));
        assert!(fnmatch("a[b", "a[b"));
        assert!(fnmatch("]", "[]]"));
        assert!(fnmatch("", "*"));
        assert!(!fnmatch("abc", "a*d"));
        assert!(fnmatch("aXbXc", "a*b*c"));
    }

    #[test]
    fn rule_forms() {
        assert_eq!(kind_of("use:tool:issue_*"), Ok(Some(Kind::Tool)));
        assert_eq!(kind_of("write:*"), Ok(None));
        assert_eq!(kind_of("*:memory:trading"), Ok(Some(Kind::Memory)));
        assert!(kind_of("write:tool:x").is_err());
        assert!(kind_of("use:path:/x").is_err());
        assert!(kind_of("write:path:rel").is_err());
        assert!(kind_of("write:path:/a/*").is_err());
        assert_eq!(kind_of("write:path:~/a"), Ok(Some(Kind::Path)));
        assert!(kind_of("use:skill:").is_err());
        assert!(kind_of("read:skill:x").is_err());
        assert!(kind_of("use").is_err());
    }

    #[test]
    fn allows_and_admin() {
        let r = Rules::new(vec!["use:skill:*".into(), "write:*".into()], vec!["use:skill:secret*".into()]);
        assert!(r.allows(Action::Use, Kind::Skill, Some("a")));
        assert!(!r.allows(Action::Use, Kind::Skill, Some("secret-x")));
        assert!(r.allows(Action::Write, Kind::Memory, None));
        assert!(!r.allows(Action::Use, Kind::Memory, None));
        assert!(r.allows_every(Action::Write));
        let denied = Rules::union([&r, &Rules::new(vec![], vec!["*:*".into()])]);
        assert!(!denied.allows_every(Action::Write));
        let p = Rules::new(vec!["write:path:/a".into(), "write:*".into()], vec!["write:path:/a/b".into()]);
        assert_eq!(p.paths(), (vec!["/a".to_owned()], vec!["/a/b".to_owned()]));
    }
}
