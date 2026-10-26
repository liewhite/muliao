import type { Agent } from './api'

export interface AgentGroups {
  /** 没有分组、没归档的 */
  ungrouped: Agent[]
  /** 有分组、没归档的，按组名 */
  groups: { name: string; agents: Agent[] }[]
  /** 归档的（不论分组），界面折叠在最后 */
  archived: Agent[]
}

/** agent 列表页的分段：不分组的平铺在前，每个组一段，归档的单独一段；各段内保持接口给的顺序（按名称） */
export function groupAgents(agents: Agent[]): AgentGroups {
  const ungrouped: Agent[] = []
  const archived: Agent[] = []
  const groups = new Map<string, Agent[]>()
  for (const a of agents) {
    if (a.archived_at !== null) archived.push(a)
    else if (a.group === null) ungrouped.push(a)
    else {
      const list = groups.get(a.group)
      if (list) list.push(a)
      else groups.set(a.group, [a])
    }
  }
  return {
    ungrouped,
    groups: [...groups].sort(([a], [b]) => a.localeCompare(b)).map(([name, list]) => ({ name, agents: list })),
    archived,
  }
}

/** 可以选来派活（新对话、指派、负责、触发器）的 agent：不含归档的；keep 是当前已选的值，归档了也留着以便显示 */
export function selectableAgents(agents: Agent[] | undefined, keep?: string | null): Agent[] {
  return (agents ?? []).filter((a) => a.archived_at === null || a.id === keep)
}
