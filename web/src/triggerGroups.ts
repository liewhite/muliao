import type { Agent, AgentId, Trigger } from './api'

export interface TriggerGroup {
  agentId: AgentId
  triggers: Trigger[]
}

/** 按 agent 分组：组的顺序同 agent 列表，列表里没有的 agent（尚未加载或已删）排在最后；组内保持原顺序 */
export function groupByAgent(triggers: Trigger[], agents: Agent[] | undefined): TriggerGroup[] {
  const groups = new Map<AgentId, Trigger[]>()
  for (const t of triggers) {
    const list = groups.get(t.agent_id)
    if (list) list.push(t)
    else groups.set(t.agent_id, [t])
  }
  const order = new Map((agents ?? []).map((a, i) => [a.id, i]))
  const rank = (id: AgentId) => order.get(id) ?? order.size
  return [...groups]
    .map(([agentId, list]) => ({ agentId, triggers: list }))
    .sort((a, b) => rank(a.agentId) - rank(b.agentId))
}
