import { useState } from 'react'
import { Alert, Button, Chip } from '@heroui/react'
import { Link } from 'react-router-dom'
import { agentRoute, api, asAgentId, type AgentId, type CatalogAsset } from '../api'
import { ActionButton, ErrorAlert, SelectInput, agentLabel, useAction, useAgents } from '../ui'

const PLATFORM = 'platform'

/** 资产归谁：平台（要收录进角色才有人能用）或某个 agent（它自己总能用）。withArchived：也列出归档的（转移时，资产可能正归它们） */
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
          ? '归平台的资产要收录进某个角色（如 base）才有 agent 能用'
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

/** 把资产转给另一个 owner：内容与收录它的角色不变，记一版 */
function TransferOwner({ asset, onDone }: { asset: Exclude<CatalogAsset, { kind: 'tool' }>; onDone: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(asset.owner)
  const action = useAction()
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <OwnerSelect value={owner} onChange={setOwner} withArchived />
      <div className="flex gap-2">
        <ActionButton
          size="sm"
          isPending={action.busy}
          isDisabled={owner === asset.owner}
          onPress={() => void action.run(() => api.transferAsset(asset.kind, asset.name, owner).then(onDone))}
        >
          转移
        </ActionButton>
      </div>
      <ErrorAlert error={action.error} />
    </div>
  )
}

/** 资产归谁、被哪些角色收录；没被收录时说明谁能用。给了 onTransferred 的可以转移 owner（内置工具归平台，不能转） */
export function AssetAccess({ asset, onTransferred }: { asset: CatalogAsset; onTransferred?: () => void }) {
  const agents = useAgents()
  const [transferring, setTransferring] = useState(false)
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        {asset.owner === null ? (
          <Chip size="sm" variant="secondary">
            平台
          </Chip>
        ) : (
          <Link to={agentRoute(asset.owner)}>
            <Chip size="sm" color="accent" variant="soft">
              {agentLabel(agents.data, asset.owner)}
            </Chip>
          </Link>
        )}
        {asset.roles.length > 0 && (
          <>
            <span className="text-muted">收录于</span>
            {asset.roles.map((r) => (
              <Link key={r} to="/roles">
                <Chip size="sm" variant="soft" className="font-mono">
                  {r}
                </Chip>
              </Link>
            ))}
          </>
        )}
        {!asset.shared && (
          <Chip size="sm" color="warning" variant="soft">
            {asset.owner === null ? '没分给任何 agent：没有 agent 能用' : '没分给别的 agent：只有 owner 能用'}
          </Chip>
        )}
        {asset.shared && asset.roles.length === 0 && <span className="text-muted">经 agent 自己的 allow 分发</span>}
        {onTransferred && asset.kind !== 'tool' && (
          <Button size="sm" variant="ghost" className="h-6 px-2 text-xs text-accent" onPress={() => setTransferring(!transferring)}>
            {transferring ? '取消转移' : '转移 owner'}
          </Button>
        )}
      </div>
      {onTransferred && asset.kind !== 'tool' && transferring && (
        <TransferOwner
          asset={asset}
          onDone={() => {
            setTransferring(false)
            onTransferred()
          }}
        />
      )}
    </div>
  )
}

/** 编辑期间别处（agent、自进化、另一个页面）改了它：草稿是按旧内容改的，保存会被拒绝（409），只能丢掉修改载入最新 */
export function StaleDraft({ what, onDiscard }: { what: string; onDiscard: () => void }) {
  return (
    <Alert status="warning">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>{what}在你编辑期间被别处改过</Alert.Title>
        <Alert.Description>你的修改是按旧内容做的，保存会被拒绝。先复制要保留的内容，再载入最新的重新改。</Alert.Description>
      </Alert.Content>
      <Button size="sm" variant="secondary" onPress={onDiscard}>
        载入最新（丢弃我的修改）
      </Button>
    </Alert>
  )
}
