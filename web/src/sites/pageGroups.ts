import type { AgentId, SitePage } from '../api'

export interface PageGroup {
  agentId: AgentId
  pages: SitePage[]
}

/**
 * 页面按 agent 分组，都按最近更新排：组内按更新时间倒序，组之间按组里最新一张的更新时间倒序
 * （先整体按时间倒序，再按首次出现的 agent 归组，组的顺序就是各组最新页面的顺序）
 */
export function groupPagesByAgent(pages: SitePage[]): PageGroup[] {
  const sorted = [...pages].sort((x, y) => Date.parse(y.modified) - Date.parse(x.modified))
  const groups = new Map<AgentId, SitePage[]>()
  for (const p of sorted) {
    const list = groups.get(p.agent_id)
    if (list) list.push(p)
    else groups.set(p.agent_id, [p])
  }
  return [...groups].map(([agentId, list]) => ({ agentId, pages: list }))
}
