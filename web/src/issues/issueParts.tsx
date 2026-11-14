// issue 的小部件：状态圆点、负责项目的色点、胶囊与下拉框（看板、issue 页、会话页的标题共用）

import type { ComponentProps } from 'react'
import { selectableAgents } from '../agents/agentGroups'
import { useAgents } from '../agents/agentList'
import type { AgentId, IssueStatus } from '../api'
import { SelectInput, type Option } from '../ui'
import { STATUS_COLOR, ownerHue, ownerLabel, ownerStyle } from './model'

export function StatusDot({ status }: { status: IssueStatus }) {
  return <span className={`inline-block size-3 shrink-0 rounded-full border-2 ${STATUS_COLOR[status]}`} aria-hidden />
}

/** 负责项目的色点；不着色时是灰点 */
export function OwnerDot({ owner }: { owner: AgentId | null }) {
  const agents = useAgents().data
  const hue = ownerHue(agents, owner)
  return (
    <span
      className={`inline-block size-2.5 shrink-0 rounded-full ${hue === null ? 'bg-muted' : 'owner-color bg-(--owner-fg)'}`}
      style={ownerStyle(hue)}
      aria-hidden
    />
  )
}

/** 负责项目的胶囊：浅色底 + owner 色文字 */
export function OwnerChip({ owner }: { owner: AgentId }) {
  const agents = useAgents().data
  const hue = ownerHue(agents, owner)
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
        hue === null ? 'bg-default text-muted' : 'owner-color bg-(--owner-bg) text-(--owner-fg)'
      }`}
      style={ownerStyle(hue)}
    >
      <OwnerDot owner={owner} />
      {ownerLabel(agents, owner)}
    </span>
  )
}

/** 选负责项目的下拉框：每个 agent 前带它的色点；leading 是排在 agent 前面的额外选项（如「所有项目」）。
 * assignable：是在给 issue 定 owner（不是筛选），归档的 agent 不列，当前值除外 */
export function OwnerSelect<L extends string = never>({
  value,
  onChange,
  leading = [],
  assignable = false,
  ...props
}: Omit<ComponentProps<typeof SelectInput>, 'options' | 'value' | 'onChange'> & {
  // 哨兵选项（如「所有项目」）的 id 只从 leading 推断，没有 leading 时只能选 agent
  value: AgentId | NoInfer<L> | null
  onChange: (value: AgentId | NoInfer<L>) => void
  leading?: Option<L>[]
  assignable?: boolean
}) {
  const agents = useAgents().data
  const listed = assignable ? selectableAgents(agents, value) : (agents ?? [])
  const options: Option<AgentId | L>[] = [
    ...leading,
    ...listed.map((a) => ({ id: a.id, label: a.name, prefix: <OwnerDot owner={a.id} /> })),
  ]
  return <SelectInput<AgentId | L> {...props} value={value} onChange={onChange} options={options} />
}
