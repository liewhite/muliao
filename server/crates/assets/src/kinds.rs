//! 种类的唯一定义：AI 资源的种类（[`Kind`]）、存成文件的种类（[`StoredKind`]），以及每种存成文件的资源的固定属性
//! （[`KindSpec`]：名字、放在哪、名字规则、说明在公共 skill 哪一节、文件怎么读、写之前怎么校验）。读树与写入口只按登记
//! 分派，不列举种类；加一种资源 = 在 [`StoredKind`] 加一项、写它的格式模块、在 [`StoredKind::spec`] 登记一行。

use std::path::Path;

use crate::formats::mcp::McpFile;
use crate::formats::memory::MemoryText;
use crate::formats::secret::SecretFile;
use crate::formats::skill::SkillDir;
use crate::formats::{Draft, Loaded, mcp, memory, secret, skill};
use crate::naming::validate_slug;

/// AI 资源的种类，也是规则 `action:kind:glob` 里的 kind。内置工具（tool）在代码里、各自注册时声明属性（工具表
/// [`crate::rules::ToolTable`]），归平台、没有内容；其余四种是资源目录里的文件。本机上的可写目录（`write:path:<目录>`，
/// #416）不是资源，是另一种规则（[`crate::rules::Rule::Path`]）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Tool,
    Skill,
    Mcp,
    Secret,
    Memory,
}

impl Kind {
    pub const ALL: [Kind; 5] = [Kind::Tool, Kind::Skill, Kind::Mcp, Kind::Secret, Kind::Memory];

    /// 错误信息里的名字（同一种类处处同一个说法）。
    pub fn label(self) -> &'static str {
        match self {
            Kind::Tool => "built-in tool",
            Kind::Skill => StoredKind::Skill.spec().label,
            Kind::Mcp => StoredKind::Mcp.spec().label,
            Kind::Secret => StoredKind::Secret.spec().label,
            Kind::Memory => StoredKind::Memory.spec().label,
        }
    }
}

/// 存成文件的四种：可以新建、改、删、转移 owner（记忆不转移）。
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, serde::Serialize, serde::Deserialize, utoipa::ToSchema,
)]
#[serde(rename_all = "snake_case")]
pub enum StoredKind {
    Skill,
    Mcp,
    Secret,
    Memory,
}

impl StoredKind {
    pub const ALL: [StoredKind; 4] = [StoredKind::Skill, StoredKind::Mcp, StoredKind::Secret, StoredKind::Memory];
    /// 按名字排（mcp、memory、secret、skill）：列出资源时的顺序
    pub const BY_NAME: [StoredKind; 4] = [StoredKind::Mcp, StoredKind::Memory, StoredKind::Secret, StoredKind::Skill];

    pub fn kind(self) -> Kind {
        match self {
            StoredKind::Skill => Kind::Skill,
            StoredKind::Mcp => Kind::Mcp,
            StoredKind::Secret => Kind::Secret,
            StoredKind::Memory => Kind::Memory,
        }
    }

    pub fn label(self) -> &'static str {
        self.spec().label
    }

    pub fn spec(self) -> &'static KindSpec {
        match self {
            StoredKind::Skill => &SKILL,
            StoredKind::Mcp => &MCP,
            StoredKind::Secret => &SECRET,
            StoredKind::Memory => &MEMORY,
        }
    }
}

muliao_platform::serde_names!(Kind, StoredKind);

/// 讲资源目录怎么改的公共 skill（#501）：写入口的拒绝与提示词指向它
pub const ASSETS_SKILL: &str = "assets-files";

/// 讲资源目录怎么改的公共 skill（assets-files）的小节；报错里指向它们，标题与 skill 里的 `## <名>` 逐字一致
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Section {
    Layout,
    Memory,
    Skills,
    McpServers,
    Secrets,
    AgentToml,
    RolesAndPermissions,
}

impl Section {
    pub fn title(self) -> &'static str {
        match self {
            Section::Layout => "Layout",
            Section::Memory => "Memory",
            Section::Skills => "Skills",
            Section::McpServers => "MCP servers",
            Section::Secrets => "Secrets",
            Section::AgentToml => "agent.toml",
            Section::RolesAndPermissions => "Roles and permissions",
        }
    }
}

/// 各种类专有的内容（load 读出的）。资源是哪一种只从这里来（[`Content::kind`]），不另存一份。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Content {
    Memory(MemoryText),
    Skill(SkillDir),
    Mcp(McpFile),
    Secret(SecretFile),
}

impl Content {
    pub fn kind(&self) -> StoredKind {
        match self {
            Content::Memory(_) => StoredKind::Memory,
            Content::Skill(_) => StoredKind::Skill,
            Content::Mcp(_) => StoredKind::Mcp,
            Content::Secret(_) => StoredKind::Secret,
        }
    }

    /// skill 与 secret 的描述（记忆、MCP 为空）。
    pub fn description(&self) -> &str {
        match self {
            Content::Skill(s) => &s.description,
            Content::Secret(s) => &s.description,
            Content::Memory(_) | Content::Mcp(_) => "",
        }
    }

    /// 能交出资源领域的部分：secret 只有 allow_shell（密文不出这个领域）。
    pub fn shown(&self) -> Shown {
        match self {
            Content::Memory(m) => Shown::Memory(m.clone()),
            Content::Skill(s) => Shown::Skill(s.clone()),
            Content::Mcp(m) => Shown::Mcp(m.clone()),
            Content::Secret(s) => Shown::Secret { allow_shell: s.allow_shell },
        }
    }
}

/// 一项资源能给领域外看的内容（界面的资源页与 agent 详情），按种类。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Shown {
    Memory(MemoryText),
    Skill(SkillDir),
    Mcp(McpFile),
    Secret { allow_shell: bool },
}

impl Shown {
    pub fn kind(&self) -> StoredKind {
        match self {
            Shown::Memory(_) => StoredKind::Memory,
            Shown::Skill(_) => StoredKind::Skill,
            Shown::Mcp(_) => StoredKind::Mcp,
            Shown::Secret { .. } => StoredKind::Secret,
        }
    }
}

/// 一种存成文件的资源的内容类型：按种类查询（[`crate::tree::Snapshot::typed`]、[`crate::access::usable`]）直接拿到它。
/// 快照里这一种的资源就是内容是这一种的那些（[`Content::kind`]），所以 [`KindContent::of`] 是按内容挑选，不是核对。
pub trait KindContent: Sized {
    const KIND: StoredKind;
    fn of(content: &Content) -> Option<&Self>;
}

impl KindContent for MemoryText {
    const KIND: StoredKind = StoredKind::Memory;
    fn of(content: &Content) -> Option<&Self> {
        if let Content::Memory(c) = content { Some(c) } else { None }
    }
}

impl KindContent for SkillDir {
    const KIND: StoredKind = StoredKind::Skill;
    fn of(content: &Content) -> Option<&Self> {
        if let Content::Skill(c) = content { Some(c) } else { None }
    }
}

impl KindContent for McpFile {
    const KIND: StoredKind = StoredKind::Mcp;
    fn of(content: &Content) -> Option<&Self> {
        if let Content::Mcp(c) = content { Some(c) } else { None }
    }
}

impl KindContent for SecretFile {
    const KIND: StoredKind = StoredKind::Secret;
    fn of(content: &Content) -> Option<&Self> {
        if let Content::Secret(c) = content { Some(c) } else { None }
    }
}

/// 一种存成文件的资源的固定属性。
pub struct KindSpec {
    /// 错误信息里的名字
    pub label: &'static str,
    /// 放在 owner 目录下哪个子目录（私有记忆不在这里：agent 目录里的 memory.md）
    pub shelf: &'static str,
    /// 一项是 `<名><suffix>` 的文件；skill 是 `<名>/` 目录（suffix 为空）
    pub suffix: &'static str,
    pub is_directory: bool,
    pub section: Section,
    /// 名字的规则：(label, 名字) → 不合时的原因
    name_rule: fn(&str, &str) -> Result<(), String>,
    /// 读一项：(文件或目录, 名字, 记忆的长度上限) → 内容，或读坏的原因（读树记成问题）
    pub load: fn(&Path, &str, usize) -> Result<Loaded, String>,
    /// 写之前：要写成的全文 → 实际存下的全文，或拒绝的原因（写入口拒绝）
    pub vet: fn(&Draft<'_>) -> Result<String, String>,
}

impl KindSpec {
    pub fn check_name(&self, name: &str) -> Result<(), String> {
        (self.name_rule)(self.label, name)
    }
}

fn slug(label: &str, name: &str) -> Result<(), String> {
    validate_slug(label, name).map_err(|e| e.message)
}

fn key(_label: &str, name: &str) -> Result<(), String> {
    secret::validate_key(name)
}

static SKILL: KindSpec = KindSpec {
    label: "skill",
    shelf: "skills",
    suffix: "",
    is_directory: true,
    section: Section::Skills,
    name_rule: slug,
    load: skill::load,
    vet: skill::vet,
};
static MCP: KindSpec = KindSpec {
    label: "MCP server",
    shelf: "mcp",
    suffix: ".json",
    is_directory: false,
    section: Section::McpServers,
    name_rule: slug,
    load: mcp::load,
    vet: mcp::vet,
};
static SECRET: KindSpec = KindSpec {
    label: "secret",
    shelf: "secrets",
    suffix: ".toml",
    is_directory: false,
    section: Section::Secrets,
    name_rule: key,
    load: secret::load,
    vet: secret::vet,
};
/// 平台的记忆（全局与共享）；私有记忆按所属 agent 认、没有名字规则
static MEMORY: KindSpec = KindSpec {
    label: "memory",
    shelf: "memory",
    suffix: ".md",
    is_directory: false,
    section: Section::Memory,
    name_rule: slug,
    load: memory::load,
    vet: memory::vet,
};
