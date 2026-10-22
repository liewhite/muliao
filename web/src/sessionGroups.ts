import type { AgentId, Schemas } from './api'

export interface SessionGroup<T> {
  agentId: AgentId
  sessions: T[]
  unread: number
}

// 按 agent 分组；组内保持原顺序（接口按最近更新排序），
// 组按各自第一次出现的位置排列，即最近活跃的 agent 在前
export function groupByAgent<T extends { agent_id: AgentId; unread: boolean }>(list: T[]): SessionGroup<T>[] {
  const groups = new Map<AgentId, SessionGroup<T>>()
  for (const s of list) {
    let g = groups.get(s.agent_id)
    if (!g) {
      g = { agentId: s.agent_id, sessions: [], unread: 0 }
      groups.set(s.agent_id, g)
    }
    g.sessions.push(s)
    if (s.unread) g.unread += 1
  }
  return [...groups.values()]
}

// 进行中（在运行、或在等后台任务 / 委派 / 用户回答）的会话单独置顶在所有分组之上，其余的再按 agent 分组；
// 两部分都保持接口顺序。接口按同一个工作状态把进行中的排在最前（看全部会话，不只这一页），这里只是切开
export function splitActive<T extends { state: Schemas['WorkState'] }>(
  list: T[],
): { active: T[]; rest: T[] } {
  return { active: list.filter((s) => s.state !== 'idle'), rest: list.filter((s) => s.state === 'idle') }
}
