import { useState } from 'react'
import { Button, Chip } from '@heroui/react'
import { Link } from 'react-router-dom'
import { agentRoute, api, asAgentId, type AgentId, type CatalogItem } from '../api'
import { ActionButton, ErrorAlert, SelectInput, agentLabel, useAction, useAgents } from '../ui'

const PLATFORM = 'platform'

/** 条目归谁：平台（要收录进角色才有人能用）或某个 agent（它自己总能用）。withArchived：也列出归档的（转移时，条目可能正归它们） */
export function OwnerSelect({
  value,
  onChange,
  withArchived = false,
}: {
  value: AgentId | null
  onChange: (owner: AgentId | null) => void
  withArchived?: boolean
}) {
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
        ...(agents.data ?? [])
          .filter((a) => withArchived || a.archived_at === null)
          .map((a) => ({ id: a.id, label: a.archived_at === null ? a.name : `${a.name}（已归档）` })),
      ]}
    />
  )
}

/** 把条目转给另一个 owner：内容与收录它的角色不变，记一版 */
function TransferOwner({ item, onDone }: { item: Exclude<CatalogItem, { kind: 'tool' }>; onDone: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(item.owner)
  const action = useAction()
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <OwnerSelect value={owner} onChange={setOwner} withArchived />
      <div className="flex gap-2">
        <ActionButton
          size="sm"
          isPending={action.busy}
          isDisabled={owner === item.owner}
          onPress={() => void action.run(() => api.transferItem(item.kind, item.name, owner).then(onDone))}
        >
          转移
        </ActionButton>
      </div>
      <ErrorAlert error={action.error} />
    </div>
  )
}

/** 条目归谁、被哪些角色收录；没被收录时说明谁能用。给了 onTransferred 的可以转移 owner（内置工具归平台，不能转） */
export function ItemAccess({ item, onTransferred }: { item: CatalogItem; onTransferred?: () => void }) {
  const agents = useAgents()
  const [transferring, setTransferring] = useState(false)
  return (
    <div className="flex flex-col gap-2">
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
        {onTransferred && item.kind !== 'tool' && (
          <Button size="sm" variant="ghost" className="h-6 px-2 text-xs text-accent" onPress={() => setTransferring(!transferring)}>
            {transferring ? '取消转移' : '转移 owner'}
          </Button>
        )}
      </div>
      {onTransferred && item.kind !== 'tool' && transferring && (
        <TransferOwner
          item={item}
          onDone={() => {
            setTransferring(false)
            onTransferred()
          }}
        />
      )}
    </div>
  )
}
