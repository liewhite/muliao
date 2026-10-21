import type { AgentId } from './api'

export interface SessionGroup<T> {
  agentId: AgentId
  sessions: T[]
  unread: number
  running: number
}

// 按 agent 分组；组内保持原顺序（接口把运行中的置顶，其余按最近更新排序），
// 组按各自第一次出现的位置排列，即有会话在运行的、最近活跃的 agent 在前
export function groupByAgent<T extends { agent_id: AgentId; unread: boolean; running: boolean }>(list: T[]): SessionGroup<T>[] {
  const groups = new Map<AgentId, SessionGroup<T>>()
  for (const s of list) {
    let g = groups.get(s.agent_id)
    if (!g) {
      g = { agentId: s.agent_id, sessions: [], unread: 0, running: 0 }
      groups.set(s.agent_id, g)
    }
    g.sessions.push(s)
    if (s.unread) g.unread += 1
    if (s.running) g.running += 1
  }
  return [...groups.values()]
}
