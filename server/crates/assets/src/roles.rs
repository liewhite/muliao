//! 角色：名字（内置的 base、admin 与自建的）、定义（[`Roles`]，内置的一定在），以及 agent 的规则怎么引用它们。
//!
//! agent 的规则有两种形态：写下的（[`AgentRules`]：角色只是名字，界面、工具交来的和要写进 access.toml 的）与对上了定义的
//! （[`BoundRules`]：只引用存在的角色，base 在最前）。算最终规则、规则来源只用后者，它只经 [`Roles::bind`] 得到，所以
//! 「引用了不存在的角色」在那一处报出，算规则时不用再查。

use std::collections::BTreeMap;
use std::fmt;

use muliao_platform::errors::DomainError;
use serde::{Deserialize, Serialize};

use crate::naming::validate_slug;
use crate::rules::Rules;

/// 自建角色的名字：slug，不是内置角色的名字。只经 [`RoleName::parse`] 得到。
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct CustomRole(String);

/// 角色名：内置的 base（每个 agent 隐含）、admin，或自建的。在 access.toml、接口与工具参数里都是名字字符串，读入时解析。
#[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(into = "String", try_from = "String")]
pub enum RoleName {
    Base,
    Admin,
    Custom(CustomRole),
}

impl RoleName {
    pub fn parse(raw: &str) -> std::result::Result<Self, DomainError> {
        Ok(match raw {
            "base" => RoleName::Base,
            "admin" => RoleName::Admin,
            _ => {
                validate_slug("role", raw)?;
                RoleName::Custom(CustomRole(raw.to_owned()))
            }
        })
    }

    pub fn as_str(&self) -> &str {
        match self {
            RoleName::Base => "base",
            RoleName::Admin => "admin",
            RoleName::Custom(c) => &c.0,
        }
    }

    /// 内置角色：不能删，规则里有代码固定的部分。
    pub fn builtin(&self) -> bool {
        match self {
            RoleName::Base | RoleName::Admin => true,
            RoleName::Custom(_) => false,
        }
    }

    /// 能写进 agent 的 roles 的形式；base 每个 agent 隐含，不能写，为 None。
    pub fn assigned(&self) -> Option<AssignedRole> {
        match self {
            RoleName::Base => None,
            RoleName::Admin => Some(AssignedRole::Admin),
            RoleName::Custom(c) => Some(AssignedRole::Custom(c.clone())),
        }
    }

    pub fn assignable(&self) -> bool {
        self.assigned().is_some()
    }

    /// 内置角色没写描述时的描述。
    pub fn default_description(&self) -> Option<&'static str> {
        match self {
            RoleName::Base => {
                Some("每个 agent 都有：全部非管理工具与全局记忆，加上这里收录的 skill、MCP server、secret、记忆")
            }
            RoleName::Admin => Some("全部工具（含管理 agent 与角色），并且能改任何 AI 资源"),
            RoleName::Custom(_) => None,
        }
    }
}

impl TryFrom<String> for RoleName {
    type Error = DomainError;

    fn try_from(raw: String) -> std::result::Result<Self, DomainError> {
        Self::parse(&raw)
    }
}

impl From<RoleName> for String {
    fn from(n: RoleName) -> String {
        n.as_str().to_owned()
    }
}

// 与名字写法相同（报错文字里 `{names:?}` 不带类型名）
impl fmt::Debug for RoleName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(self.as_str(), f)
    }
}

impl fmt::Display for RoleName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

// 接口描述里是名字字符串
impl utoipa::PartialSchema for RoleName {
    fn schema() -> utoipa::openapi::RefOr<utoipa::openapi::schema::Schema> {
        <String as utoipa::PartialSchema>::schema()
    }
}

impl utoipa::ToSchema for RoleName {}

muliao_platform::json_schema_as!(RoleName, String);

/// agent 的 roles 里能写的角色：除了每个 agent 隐含的 base。在接口、工具参数与 access.toml 里都是名字字符串，写 base
/// 在读入时就报错（access.toml 里只记一条问题、略过，见 [`crate::access_file`]）。
#[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(into = "String", try_from = "String")]
pub enum AssignedRole {
    Admin,
    Custom(CustomRole),
}

impl AssignedRole {
    pub fn parse(raw: &str) -> std::result::Result<Self, DomainError> {
        RoleName::parse(raw)?.assigned().ok_or_else(|| {
            DomainError::invalid(format!("role {raw:?} is implicit for every agent; do not list it in roles"))
        })
    }

    pub fn name(&self) -> RoleName {
        match self {
            AssignedRole::Admin => RoleName::Admin,
            AssignedRole::Custom(c) => RoleName::Custom(c.clone()),
        }
    }
}

impl TryFrom<String> for AssignedRole {
    type Error = DomainError;

    fn try_from(raw: String) -> std::result::Result<Self, DomainError> {
        Self::parse(&raw)
    }
}

impl From<AssignedRole> for String {
    fn from(r: AssignedRole) -> String {
        r.name().as_str().to_owned()
    }
}

impl fmt::Debug for AssignedRole {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Debug::fmt(&self.name(), f)
    }
}

impl fmt::Display for AssignedRole {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.name(), f)
    }
}

impl utoipa::PartialSchema for AssignedRole {
    fn schema() -> utoipa::openapi::RefOr<utoipa::openapi::schema::Schema> {
        <String as utoipa::PartialSchema>::schema()
    }
}

impl utoipa::ToSchema for AssignedRole {}

muliao_platform::json_schema_as!(AssignedRole, String);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Role {
    pub name: RoleName,
    pub description: String,
    /// 可编辑的规则（access.toml 的 [roles.<名>]）
    pub rules: Rules,
    /// 内置角色在代码里固定的规则（自建角色为空）
    pub fixed: Rules,
}

impl Role {
    pub fn effective(&self) -> Rules {
        Rules::union([&self.fixed, &self.rules])
    }
}

/// 全部角色：内置的两个一定在，自建的按名称。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Roles {
    base: Role,
    admin: Role,
    custom: BTreeMap<CustomRole, Role>,
}

impl Roles {
    /// 只有内置角色：固定的规则，描述用默认的（access.toml 还没读成功过时就是它们）。
    pub fn builtin(base: Rules, admin: Rules) -> Self {
        let role = |name: RoleName, fixed: Rules| Role {
            description: name.default_description().unwrap_or_default().to_owned(),
            name,
            rules: Rules::default(),
            fixed,
        };
        Self { base: role(RoleName::Base, base), admin: role(RoleName::Admin, admin), custom: BTreeMap::new() }
    }

    pub fn get(&self, name: &RoleName) -> Option<&Role> {
        match name {
            RoleName::Base => Some(&self.base),
            RoleName::Admin => Some(&self.admin),
            RoleName::Custom(c) => self.custom.get(c),
        }
    }

    pub fn base(&self) -> &Role {
        &self.base
    }

    /// 内置的在前（base、admin），自建的按名称。
    pub fn values(&self) -> impl Iterator<Item = &Role> {
        [&self.base, &self.admin].into_iter().chain(self.custom.values())
    }

    /// 写下一个角色的定义：内置角色只换描述与可编辑的规则（固定的部分不变），自建的整个换掉或加上。
    pub(crate) fn define(&mut self, name: RoleName, description: String, rules: Rules) {
        let slot = match name {
            RoleName::Base => &mut self.base,
            RoleName::Admin => &mut self.admin,
            RoleName::Custom(c) => {
                let role = Role { name: RoleName::Custom(c.clone()), description, rules, fixed: Rules::default() };
                self.custom.insert(c, role);
                return;
            }
        };
        slot.description = description;
        slot.rules = rules;
    }

    /// 写下的规则对上角色的定义。引用了没定义的角色 InvalidInput；写重复的只是多余，略过。
    pub fn bind(&self, rules: &AgentRules) -> Result<BoundRules, DomainError> {
        let mut assigned: Vec<(AssignedRole, Role)> = Vec::new();
        let mut unknown = Vec::new();
        for name in &rules.roles {
            match self.get(&name.name()) {
                Some(_) if assigned.iter().any(|(n, _)| n == name) => {}
                Some(role) => assigned.push((name.clone(), role.clone())),
                None => unknown.push(name),
            }
        }
        if !unknown.is_empty() {
            return Err(DomainError::invalid(format!("roles {unknown:?} are not defined")));
        }
        Ok(BoundRules { base: self.base.clone(), assigned, own: rules.own() })
    }

    /// 只有 base 的规则（access.toml 里没写的 agent）。
    pub fn base_only(&self) -> BoundRules {
        BoundRules { base: self.base.clone(), assigned: Vec::new(), own: Rules::default() }
    }
}

/// agent 写下的分发规则：角色（不含每个 agent 隐含的 base）与它自己的 allow / deny。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AgentRules {
    pub roles: Vec<AssignedRole>,
    pub allow: Vec<crate::rules::Rule>,
    pub deny: Vec<crate::rules::Rule>,
}

impl AgentRules {
    /// 它自己的 allow / deny（不含角色）。
    pub fn own(&self) -> Rules {
        Rules::new(self.allow.clone(), self.deny.clone())
    }
}

/// agent 的规则对上了角色的定义（[`Roles::bind`]）：base 在最前，再按它的角色，加上它自己的 allow / deny。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BoundRules {
    base: Role,
    assigned: Vec<(AssignedRole, Role)>,
    own: Rules,
}

impl BoundRules {
    /// 最终规则的各个来源：base 在前、再按它的角色，最后是它自己的 allow / deny。
    pub fn sources(&self) -> Vec<(RuleSource, Rules)> {
        std::iter::once(&self.base)
            .chain(self.assigned.iter().map(|(_, r)| r))
            .map(|r| (RuleSource::Role(r.name.clone()), r.effective()))
            .chain(std::iter::once((RuleSource::Own, self.own.clone())))
            .collect()
    }

    /// 最终规则：各来源的并集。
    pub fn effective(&self) -> Rules {
        Rules::union(self.sources().iter().map(|(_, r)| r))
    }

    /// 带这个角色（base 人人都有）。
    pub fn holds(&self, name: &RoleName) -> bool {
        match name.assigned() {
            None => true,
            Some(a) => self.assigned.iter().any(|(n, _)| *n == a),
        }
    }

    /// 写下的形态（不含隐含的 base）：改规则时从它出发。
    pub fn written(&self) -> AgentRules {
        AgentRules {
            roles: self.assigned.iter().map(|(n, _)| n.clone()).collect(),
            allow: self.own.allow.clone(),
            deny: self.own.deny.clone(),
        }
    }
}

/// 一组规则从哪里来：某个角色（含 base），或 agent 自己的 allow / deny。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RuleSource {
    Role(RoleName),
    Own,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rules::Rule;

    fn rules(allow: &[&str]) -> Rules {
        Rules::new(allow.iter().map(|p| Rule::parse(p).unwrap()).collect(), vec![])
    }

    #[test]
    fn names_parse_into_builtin_or_custom() {
        assert_eq!(RoleName::parse("base").unwrap(), RoleName::Base);
        assert_eq!(RoleName::parse("admin").unwrap(), RoleName::Admin);
        let custom = RoleName::parse("trading").unwrap();
        assert!(!custom.builtin() && custom.assignable());
        assert!(!RoleName::Base.assignable());
        assert!(RoleName::parse("Bad Name").is_err());
        assert_eq!(serde_json::to_string(&custom).unwrap(), "\"trading\"");
        assert_eq!(format!("{custom:?}"), "\"trading\"");
        let err = serde_json::from_str::<AssignedRole>("\"base\"").unwrap_err().to_string();
        assert!(err.contains("implicit"), "{err}");
        assert_eq!(serde_json::from_str::<AssignedRole>("\"admin\"").unwrap(), AssignedRole::Admin);
    }

    #[test]
    fn binding_refuses_undefined_roles_and_puts_base_first() {
        let mut roles = Roles::builtin(rules(&["use:tool:a"]), rules(&["use:tool:*"]));
        roles.define(RoleName::parse("r").unwrap(), "d".into(), rules(&["use:skill:s"]));
        let r = AssignedRole::parse("r").unwrap();
        let written = AgentRules { roles: vec![r.clone(), r.clone()], ..Default::default() };
        let bound = roles.bind(&written).unwrap();
        let names: Vec<RuleSource> = bound.sources().into_iter().map(|(s, _)| s).collect();
        assert_eq!(names, [RuleSource::Role(RoleName::Base), RuleSource::Role(r.name()), RuleSource::Own]);
        assert!(bound.holds(&RoleName::Base) && bound.holds(&r.name()) && !bound.holds(&RoleName::Admin));
        assert_eq!(bound.written().roles, [r]);
        let missing = AgentRules { roles: vec![AssignedRole::parse("nope").unwrap()], ..Default::default() };
        assert!(roles.bind(&missing).is_err());
    }
}
