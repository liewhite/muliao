import { Chip } from '@heroui/react'
import { Link } from 'react-router-dom'
import { agentRoute, asAgentId, type AgentId, type CatalogItem } from '../api'
import { SelectInput, agentLabel, useAgents } from '../ui'

const PLATFORM = 'platform'

/** 新建条目归谁：平台（要收录进角色才有人能用）或某个 agent（它自己总能用） */
export function OwnerSelect({ value, onChange }: { value: AgentId | null; onChange: (owner: AgentId | null) => void }) {
  const agents = useAgents()
  return (
    <SelectInput
      label="归属（owner）"
      hint={
        value === null
          ? '归平台的条目要收录进某个角色（如 base）才有 agent 能用'
          : '这个 agent 自己总能用；要给别的 agent，把它收录进角色'
      }
      value={value ?? PLATFORM}
      onChange={(v) => onChange(v === PLATFORM ? null : asAgentId(v))}
      options={[
        { id: PLATFORM, label: '平台' },
        ...(agents.data ?? []).filter((a) => a.archived_at === null).map((a) => ({ id: a.id, label: a.name })),
      ]}
    />
  )
}

/** 条目归谁、被哪些角色收录；没被收录时说明谁能用 */
export function ItemAccess({ item }: { item: CatalogItem }) {
  const agents = useAgents()
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      {item.owner === null ? (
        <Chip size="sm" variant="secondary">
          平台
        </Chip>
      ) : (
        <Link to={agentRoute(item.owner)}>
          <Chip size="sm" color="accent" variant="soft">
            {agentLabel(agents.data, item.owner)}
          </Chip>
        </Link>
      )}
      {item.roles.length === 0 ? (
        <Chip size="sm" color="warning" variant="soft">
          {item.owner === null ? '未收录进角色：没有 agent 能用' : '未收录进角色：只有 owner 能用'}
        </Chip>
      ) : (
        <>
          <span className="text-muted">收录于</span>
          {item.roles.map((r) => (
            <Link key={r} to="/roles">
              <Chip size="sm" variant="soft" className="font-mono">
                {r}
              </Chip>
            </Link>
          ))}
        </>
      )}
    </div>
  )
}
