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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Action {
    Use,
    Write,
}

impl Action {
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

/// 当前配置下注册的内置工具，按注册顺序。工具集要用资源目录与分发构造，资源目录又要它：组合根先建一张待填的表
/// （[`ToolTable::pending`]），装好工具集后用拿到的 [`TableBinder`] 填一次（它被消耗，填不了第二次）；组合期间不读快照，
/// 填之前用到是组合根的 bug，启动时当场 panic。
#[derive(Debug, Default)]
pub struct ToolTable {
    specs: OnceLock<Vec<ToolSpec>>,
}

/// 填一张待填的工具表的唯一凭据。
pub struct TableBinder(std::sync::Arc<ToolTable>);

impl TableBinder {
    pub fn bind(self, specs: Vec<ToolSpec>) {
        self.0.specs.set(specs).expect("a binder fills its table once");
    }
}

impl ToolTable {
    /// 一张待填的表与填它的凭据。
    pub fn pending() -> (std::sync::Arc<ToolTable>, TableBinder) {
        let table = std::sync::Arc::new(Self::default());
        (table.clone(), TableBinder(table))
    }

    pub fn of(specs: Vec<ToolSpec>) -> Self {
        Self { specs: OnceLock::from(specs) }
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

/// 一个权限：`action:kind:name`（给人看的说法，如拒绝时告诉它该申请哪条规则）。
pub fn permission_of(action: Action, kind: Kind, name: &str) -> String {
    format!("{}:{kind}:{name}", action.as_str())
}

/// 这种资源能不能写（改、删）：内置工具只有 use。
fn writable(kind: Kind) -> bool {
    match kind {
        Kind::Tool => false,
        Kind::Skill | Kind::Mcp | Kind::Secret | Kind::Memory => true,
    }
}

/// 规则补全的候选（界面填规则时用）：每种的 `use:kind:*` 在前，然后每项资产（kind 与它在规则里的名字，按给的顺序）的
/// `use:kind:name`，能写的种类再加 `*:kind:name`（能用也能改）。
pub fn rule_candidates<'a>(assets: impl IntoIterator<Item = (Kind, &'a str)>) -> Vec<String> {
    let every = Kind::ALL.map(|k| permission_of(Action::Use, k, "*"));
    let named = assets.into_iter().flat_map(|(kind, name)| {
        let both = writable(kind).then(|| format!("{}:{kind}:{name}", Actions::Any.as_str()));
        std::iter::once(permission_of(Action::Use, kind, name)).chain(both)
    });
    every.into_iter().chain(named).collect()
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

/// 规则里的动作：use、write，或 `*`（两者）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Actions {
    One(Action),
    Any,
}

impl Actions {
    fn covers(self, action: Action) -> bool {
        match self {
            Actions::One(a) => a == action,
            Actions::Any => true,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Actions::One(a) => a.as_str(),
            Actions::Any => "*",
        }
    }
}

/// 一条规则，读入时解析一次（[`Rule::parse`]），之后只按变体判断；写回文件与给人看时是原样的字符串（Display）。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Rule {
    /// `action:*`：不限种类（`write:*` 就是 admin）；不含本机可写目录
    Every(Actions),
    /// `action:kind:glob`：按名字通配某一种（tool 只有 use）
    Named { actions: Actions, kind: Kind, glob: String },
    /// `write:path:<目录>`：本机上可写的目录（字面，绝对或 `~/` 开头，不带通配符）
    Path(String),
}

const ACTIONS: [&str; 3] = ["use", "write", "*"];
const PATH: &str = "path";

impl Rule {
    /// 不是 `action:kind:glob`（action 为 use、write 或 *，kind 为 [`Kind`] 之一或 path，glob 非空）或 `action:*`
    /// 形式时报错（原因）；tool 只有 use；path 只有 `write:path:<字面目录>`。
    pub fn parse(pattern: &str) -> Result<Rule, String> {
        let malformed = || {
            let mut kinds: Vec<&str> = Kind::ALL.map(Kind::as_str).to_vec();
            kinds.push(PATH);
            format!(
                "rule {pattern:?} must look like 'action:kind:name' with action one of {ACTIONS:?} and kind one of \
                 {kinds:?} (fnmatch wildcards allowed in the name), or 'action:*'; e.g. 'use:tool:issue_*', \
                 'use:skill:relay-host', '*:memory:trading', 'write:*'"
            )
        };
        let (action, rest) = pattern.split_once(':').ok_or_else(malformed)?;
        let actions = match action {
            "use" => Actions::One(Action::Use),
            "write" => Actions::One(Action::Write),
            "*" => Actions::Any,
            _ => return Err(malformed()),
        };
        if rest == "*" {
            return Ok(Rule::Every(actions));
        }
        let (kind, glob) = rest.split_once(':').ok_or_else(malformed)?;
        if glob.is_empty() {
            return Err(malformed());
        }
        if kind == PATH {
            if actions != Actions::One(Action::Write)
                || !(glob.starts_with('/') || glob.starts_with("~/"))
                || glob.contains(['*', '?', '['])
            {
                return Err(format!(
                    "rule {pattern:?}: a writable path rule is 'write:path:<directory>' with an absolute or '~/' path \
                     and no wildcards, e.g. 'write:path:~/projects/arb'; it lets the agent write that directory and \
                     everything under it"
                ));
            }
            return Ok(Rule::Path(glob.to_owned()));
        }
        let kind = Kind::parse(kind).ok_or_else(malformed)?;
        if !writable(kind) && actions == Actions::One(Action::Write) {
            return Err(format!("rule {pattern:?}: built-in tools can only be used ('use:tool:...')"));
        }
        Ok(Rule::Named { actions, kind, glob: glob.to_owned() })
    }

    /// 一组规则（界面、工具参数交来的）：任一条不合法就 InvalidInput，说明每一处。
    pub fn parse_all(patterns: &[String]) -> muliao_platform::errors::Result<Vec<Rule>> {
        let (ok, bad): (Vec<_>, Vec<_>) = patterns.iter().map(|p| Rule::parse(p)).partition(Result::is_ok);
        if !bad.is_empty() {
            let reasons: Vec<String> = bad.into_iter().filter_map(Result::err).collect();
            return Err(muliao_platform::errors::invalid(reasons.join("; ")));
        }
        Ok(ok.into_iter().filter_map(Result::ok).collect())
    }

    /// 这条 allow 放行 target 上的这个动作（不限种类的也算；本机目录规则不管资源）。私有记忆没有规则里的名字，只有不限种类的
    /// 放行它。
    fn grants(&self, action: Action, target: Target<'_>) -> bool {
        match (self, target) {
            (Rule::Every(a), _) => a.covers(action),
            (Rule::Named { actions, kind: k, glob }, Target::Named { kind, name }) => {
                actions.covers(action) && *k == kind && fnmatch(name, glob)
            }
            (Rule::Named { .. }, Target::PrivateMemory) | (Rule::Path(_), _) => false,
        }
    }

    /// 这条 deny 拦 target 上的这个动作：同 [`Rule::grants`]，另外拦这一种全部名字的（glob 全是 `*`，如
    /// `write:memory:*`）也拦私有记忆；`write:memory:?`、`write:memory:a*` 拦不到它。
    fn blocks(&self, action: Action, target: Target<'_>) -> bool {
        match (self, target) {
            (Rule::Named { actions, kind, glob }, Target::PrivateMemory) => {
                actions.covers(action) && *kind == Kind::Memory && glob.chars().all(|c| c == '*')
            }
            _ => self.grants(action, target),
        }
    }

    fn every(&self, action: Action) -> bool {
        matches!(self, Rule::Every(a) if a.covers(action))
    }

    /// 一条 tool 规则匹配不上 tools 里的任何工具（拼错、工具改了名，或这次配置没注册它）。
    pub fn matches_no_tool(&self, tools: &ToolTable) -> bool {
        matches!(self, Rule::Named { kind: Kind::Tool, .. })
            && !tools.names().any(|t| self.grants(Action::Use, Target::tool(t)))
    }

    /// 按名字指某一种的规则会拼错；不限种类的与目录规则（目录可以还没建）不查。addressable 是规则能指的全部
    /// (种类, 名字)，一项都匹配不上时为真。
    pub fn matches_nothing(&self, addressable: &[(Kind, String)]) -> bool {
        matches!(self, Rule::Named { kind, glob, .. } if !addressable.iter().any(|(k, n)| k == kind && fnmatch(n, glob)))
    }
}

impl std::str::FromStr for Rule {
    type Err = String;
    fn from_str(s: &str) -> Result<Rule, String> {
        Rule::parse(s)
    }
}

impl std::fmt::Display for Rule {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Rule::Every(a) => write!(f, "{}:*", a.as_str()),
            Rule::Named { actions, kind, glob } => write!(f, "{}:{kind}:{glob}", actions.as_str()),
            Rule::Path(dir) => write!(f, "write:{PATH}:{dir}"),
        }
    }
}

/// 规则写成字符串（写进 access.toml、交给界面与工具结果）。
pub fn texts(rules: &[Rule]) -> Vec<String> {
    rules.iter().map(Rule::to_string).collect()
}

/// 规则判断的对象：按名字认的一项（内置工具或按名字认的资源），或某个 agent 的私有记忆。私有记忆按所属的 agent 认、
/// 在规则里没有名字：只有不限种类的 allow（`write:*`）放行它，deny 另有拦记忆全部名字的（`write:memory:*`）拦得到它。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Target<'a> {
    Named { kind: Kind, name: &'a str },
    PrivateMemory,
}

impl<'a> Target<'a> {
    pub fn tool(name: &'a str) -> Self {
        Target::Named { kind: Kind::Tool, name }
    }
}

/// 一组 allow / deny 规则；deny 优先。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Rules {
    pub allow: Vec<Rule>,
    pub deny: Vec<Rule>,
}

fn push_unique(into: &mut Vec<Rule>, from: &[Rule]) {
    for p in from {
        if !into.contains(p) {
            into.push(p.clone());
        }
    }
}

impl Rules {
    pub fn new(allow: Vec<Rule>, deny: Vec<Rule>) -> Self {
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

    /// 这组规则放行这个权限：allow 匹配、deny 没拦。全系统唯一的判断（私有记忆怎么配见 [`Target`]）。
    pub fn allows(&self, action: Action, target: Target<'_>) -> bool {
        permits(&self.allow, &self.deny, action, target)
    }

    /// 不限种类的规则（`write:*`、`*:*`）放行这个动作、也没有不限种类的 deny 拦它：对一切资产都能做。写上就是 admin。
    pub fn allows_every(&self, action: Action) -> bool {
        self.allow.iter().any(|r| r.every(action)) && !self.deny.iter().any(|r| r.every(action))
    }

    /// 收录：放行 use。
    pub fn includes(&self, target: Target<'_>) -> bool {
        self.allows(Action::Use, target)
    }

    /// `write:path:<目录>` 规则里的目录：(allow 的, deny 的)，按规则顺序。
    pub fn paths(&self) -> (Vec<String>, Vec<String>) {
        let pick = |v: &[Rule]| {
            v.iter()
                .filter_map(|r| match r {
                    Rule::Path(dir) => Some(dir.clone()),
                    Rule::Every(_) | Rule::Named { .. } => None,
                })
                .collect()
        };
        (pick(&self.allow), pick(&self.deny))
    }

    /// available（内置工具的名字）里放行的，按 available 的顺序。
    pub fn tools<'a>(&self, available: impl IntoIterator<Item = &'a str>) -> Vec<&'a str> {
        available.into_iter().filter(|n| self.includes(Target::tool(n))).collect()
    }
}

/// [`Rules::allows`] 的判断本身：allow 与 deny 可以来自不同的规则（某一来源的 allow 对最终的 deny，[`crate::access::Grants`]）。
pub fn permits(allow: &[Rule], deny: &[Rule], action: Action, target: Target<'_>) -> bool {
    allow.iter().any(|r| r.grants(action, target)) && !deny.iter().any(|r| r.blocks(action, target))
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
    let named = |kind, glob: &str| Rule::Named { actions: Actions::One(Action::Use), kind, glob: glob.to_owned() };
    let mut base: Vec<Rule> = tools.specs().iter().filter(|t| !t.admin).map(|t| named(Kind::Tool, &t.name)).collect();
    base.push(named(Kind::Memory, GLOBAL_MEMORY));
    let admin = vec![named(Kind::Tool, "*"), Rule::Every(Actions::One(Action::Write))];
    [(BASE, Rules::new(base, vec![])), (ADMIN, Rules::new(admin, vec![]))]
}

/// agent 的分发规则：角色（不含每个 agent 隐含的 base）与它自己的 allow / deny。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentRules {
    pub roles: Vec<String>,
    pub allow: Vec<Rule>,
    pub deny: Vec<Rule>,
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

    /// 最终规则的各个来源：base 在前、再按它的 roles，最后是它自己的 allow / deny。
    /// 不存在的角色读树时已跳过，这里再遇到是代码错误。
    pub fn sources(&self, known: &Roles) -> Vec<(RuleSource, Rules)> {
        let missing: Vec<&String> = self.roles.iter().filter(|r| !known.contains_key(*r)).collect();
        assert!(missing.is_empty(), "agent references unknown roles {missing:?}");
        std::iter::once(BASE)
            .chain(self.roles.iter().map(String::as_str))
            .map(|r| (RuleSource::Role(r.to_owned()), known[r].effective()))
            .chain(std::iter::once((RuleSource::Own, self.own())))
            .collect()
    }
}

/// 一组规则从哪里来：某个角色（含 base），或 agent 自己的 allow / deny。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuleSource {
    Role(String),
    Own,
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

    fn rules(patterns: &[&str]) -> Vec<Rule> {
        patterns.iter().map(|p| Rule::parse(p).unwrap()).collect()
    }

    #[test]
    fn candidates_are_valid_rules() {
        let got = rule_candidates([(Kind::Tool, "run_shell"), (Kind::Skill, "relay-host")]);
        assert_eq!(
            got,
            [
                "use:tool:*",
                "use:skill:*",
                "use:mcp:*",
                "use:secret:*",
                "use:memory:*",
                "use:tool:run_shell",
                "use:skill:relay-host",
                "*:skill:relay-host",
            ]
        );
        assert!(got.iter().all(|r| Rule::parse(r).is_ok()));
    }

    #[test]
    fn rule_forms() {
        for ok in ["use:tool:issue_*", "write:*", "*:memory:trading", "write:path:~/a", "*:*", "use:skill:[a-c]"] {
            // 读入再写回是原样
            assert_eq!(Rule::parse(ok).map(|r| r.to_string()), Ok(ok.to_owned()));
        }
        assert_eq!(Rule::parse("write:path:~/a"), Ok(Rule::Path("~/a".into())));
        assert_eq!(Rule::parse("write:*"), Ok(Rule::Every(Actions::One(Action::Write))));
        for bad in [
            "write:tool:x",
            "use:path:/x",
            "write:path:rel",
            "write:path:/a/*",
            "write:path:",
            "use:skill:",
            "read:skill:x",
            "use",
            "use:*:x",
            "use:sk*:x",
        ] {
            assert!(Rule::parse(bad).is_err(), "{bad}");
        }
        assert!(Rule::parse_all(&["use:skill:a".into(), "bad".into()]).is_err());
    }

    #[test]
    fn allows_and_admin() {
        let skill = |name| Target::Named { kind: Kind::Skill, name };
        let r = Rules::new(rules(&["use:skill:*", "write:*"]), rules(&["use:skill:secret*"]));
        assert!(r.allows(Action::Use, skill("a")));
        assert!(!r.allows(Action::Use, skill("secret-x")));
        assert!(r.allows(Action::Write, Target::PrivateMemory));
        assert!(!r.allows(Action::Use, Target::PrivateMemory));
        assert!(r.allows_every(Action::Write));
        let denied = Rules::union([&r, &Rules::new(vec![], rules(&["*:*"]))]);
        assert!(!denied.allows_every(Action::Write));
        // 私有记忆没有名字：拦记忆全部名字的 deny 管得到它，按名字拦的（含 `?` 这种单字符通配）管不到
        let no_memory = Rules::union([&r, &Rules::new(vec![], rules(&["write:memory:*"]))]);
        assert!(!no_memory.allows(Action::Write, Target::PrivateMemory));
        assert!(no_memory.allows(Action::Write, skill("x")));
        for by_name in ["write:memory:t*", "write:memory:?", "write:memory:[a-z]*"] {
            let by_name = Rules::union([&r, &Rules::new(vec![], rules(&[by_name]))]);
            assert!(by_name.allows(Action::Write, Target::PrivateMemory));
        }
        // 按种类放行全部名字的 allow 不放行私有记忆（只有不限种类的放行它）
        assert!(!Rules::new(rules(&["write:memory:*"]), vec![]).allows(Action::Write, Target::PrivateMemory));
        // `*` 动作两种都管；目录规则不管资源
        let any = Rules::new(rules(&["*:skill:?", "write:path:/a"]), vec![]);
        assert!(any.allows(Action::Write, skill("a")) && any.allows(Action::Use, skill("b")));
        assert!(!any.allows(Action::Use, skill("ab")));
        assert!(!any.allows_every(Action::Write));
        let p = Rules::new(rules(&["write:path:/a", "write:*"]), rules(&["write:path:/a/b"]));
        assert_eq!(p.paths(), (vec!["/a".to_owned()], vec!["/a/b".to_owned()]));
    }

    #[test]
    fn tool_and_name_checks() {
        let tools = ToolTable::of(vec![ToolSpec { name: "issue_list".into(), access: ToolAccess::Read, admin: false }]);
        assert!(!Rule::parse("use:tool:issue_*").unwrap().matches_no_tool(&tools));
        assert!(Rule::parse("use:tool:nope").unwrap().matches_no_tool(&tools));
        assert!(!Rule::parse("use:skill:nope").unwrap().matches_no_tool(&tools));
        let addressable = vec![(Kind::Skill, "s".to_owned())];
        assert!(!Rule::parse("write:skill:s*").unwrap().matches_nothing(&addressable));
        assert!(Rule::parse("use:skill:typo").unwrap().matches_nothing(&addressable));
        assert!(!Rule::parse("write:*").unwrap().matches_nothing(&addressable));
        assert!(!Rule::parse("write:path:/x").unwrap().matches_nothing(&addressable));
    }
}
