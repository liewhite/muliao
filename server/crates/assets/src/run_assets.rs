//! 一次运行要的这个 agent 的资源：配置（agent.toml）、规则、记忆、skill 索引、要连的 MCP server、与它相关没加载的文件，
//! 以及它的工作目录、资源树的位置与记忆上限。全部出自同一份快照：运行开头取一次（[`crate::service::AgentService::run_assets`]），
//! 整次运行（含其间的压缩、压缩后重新组装）都用它，不会一部分是改之前的、一部分是改之后的，也不为每一样各扫一遍资源目录。

use std::path::PathBuf;

use crate::access::{Actor, grants_for};
use crate::mcp::{McpServer, servers_of};
use crate::memory::{MemoryBlock, blocks_of};
use crate::prompt;
use crate::rules::Rules;
use crate::skills::{SkillMeta, index_of};
use crate::tree::{AgentDir, Problem, Snapshot};
use muliao_platform::errors::Result;
use muliao_platform::ids::AgentId;

#[derive(Debug, Clone)]
pub struct RunAssets {
    pub agent: AgentDir,
    /// agent 的工作目录（文件工具与 shell 的 cwd、MCP server 的工作目录）
    pub workspace: PathBuf,
    /// 最终规则（base + 角色 + 自己的 allow / deny）：开放哪些内置工具
    pub rules: Rules,
    pub memories: Vec<MemoryBlock>,
    pub skills: Vec<SkillMeta>,
    /// 能用的、已启用的 MCP server
    pub mcp: Vec<McpServer>,
    /// 资源目录里与它相关、没加载的文件（读坏、名字冲突）
    pub problems: Vec<Problem>,
    /// 整棵资源树的根
    assets_root: PathBuf,
    /// 每块记忆的字符上限
    memory_limit: usize,
}

impl RunAssets {
    /// agent 没加载（agent.toml 读坏等）时 NotFound，带上原因：要它的运行当场失败，不沿用旧值。
    pub(crate) fn of(
        snapshot: &Snapshot,
        agent_id: &AgentId,
        workspace: PathBuf,
        assets_root: PathBuf,
        memory_limit: usize,
    ) -> Result<Self> {
        let agent = snapshot.loaded_agent(agent_id)?.clone();
        let grants = grants_for(snapshot, &Actor::Agent(agent_id.to_owned()));
        Ok(Self {
            agent,
            workspace,
            rules: grants.rules().clone(),
            memories: blocks_of(snapshot, &grants, agent_id),
            skills: index_of(snapshot, &grants),
            mcp: servers_of(snapshot, &grants),
            problems: snapshot.problems_for(agent_id).into_iter().cloned().collect(),
            assets_root,
            memory_limit,
        })
    }

    /// 指令里讲资源的几段（记忆、skill 索引、Self-improvement），按这个顺序拼进指令。
    pub fn instruction_sections(&self) -> [String; 3] {
        [
            prompt::memory_section(&self.memories, self.memory_limit),
            prompt::skill_index(&self.skills, &self.agent.id),
            prompt::self_improvement(self.memory_limit, &self.agent.path, &self.assets_root),
        ]
    }

    /// 运行开头要告诉 agent 的问题：与它相关、没加载的资源文件，一条一句。
    pub fn notices(&self) -> Vec<String> {
        self.problems.iter().map(prompt::problem_notice).collect()
    }
}
