// agent 列表：选择器、id→名称显示等各处共用一份，不各自请求、不逐层传递

import { api, type Agent, type AgentId } from '../api'
import { Loader, useLoader, type LoadState } from '../load'
import { agentLabel } from './agentGroups'

const agentList = new Loader(() => api.agents())

/** agent 列表（各处共用一份，随 agents 的变化重新拉取） */
export function useAgents(): LoadState<Agent[]> {
  return useLoader(agentList)
}

/** agent 的显示名（见 agentGroups.agentLabel），不用自己拿 agent 列表 */
export function useAgentName(id: AgentId): string {
  return agentLabel(useAgents().data, id)
}

/** agent 的显示名，在 JSX 里直接用（列表里逐项、条件分支里也能用，不受 hook 调用位置的限制） */
export function AgentName({ id }: { id: AgentId }) {
  return <>{useAgentName(id)}</>
}
