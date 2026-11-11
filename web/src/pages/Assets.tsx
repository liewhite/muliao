import { Card, Tabs, type Key } from '@heroui/react'
import { useSearchParams } from 'react-router-dom'
import { api, asAgentId, type CatalogAsset, type AssetKind, type AssetOf } from '../api'
import { KIND_LABEL, KINDS, filterAssets, isKind, type OwnerFilter } from '../rules'
import { AssetAccess } from './assetParts'
import { McpTab } from './Mcp'
import { MemoryTab } from './Memory'
import { ToolLine } from './Permissions'
import { SecretTab } from './Vault'
import { SkillTab } from './Skills'
import { EmptyHint, Loading, Page, SelectInput, useAgents } from '../ui'
import { useLoad } from '../load'

/** 内置工具：归平台、代码里定义，不能改；只看它被哪些角色收录 */
function ToolTab({ assets }: { assets: AssetOf<'tool'>[] }) {
  return assets.length === 0 ? (
    <EmptyHint>没有工具</EmptyHint>
  ) : (
    <div className="flex flex-col gap-2">
      {assets.map((i) => (
        <Card key={i.name}>
          <Card.Content className="flex flex-col gap-1.5 text-sm">
            <ToolLine tool={i} />
            <AssetAccess asset={i} />
          </Card.Content>
        </Card>
      ))}
    </div>
  )
}

function KindTab({ kind, assets, owner }: { kind: AssetKind; assets: CatalogAsset[]; owner: OwnerFilter }) {
  switch (kind) {
    case 'tool':
      return <ToolTab assets={filterAssets(assets, 'tool', owner)} />
    case 'skill':
      return <SkillTab assets={filterAssets(assets, 'skill', owner)} />
    case 'mcp':
      return <McpTab assets={filterAssets(assets, 'mcp', owner)} />
    case 'secret':
      return <SecretTab assets={filterAssets(assets, 'secret', owner)} />
    case 'memory':
      return <MemoryTab assets={filterAssets(assets, 'memory', owner)} />
  }
}

function ownerFilterOf(value: string | null): OwnerFilter {
  if (value === null || value === 'all') return 'all'
  return value === 'platform' ? 'platform' : asAgentId(value)
}

/**
 * AI 资产池：全部工具、skill、MCP server、secret、记忆，不分公共私有。每项显示 owner（平台或某个 agent）与收录它的角色；
 * agent 能用、能改的 = 它自己的 + 它的角色（含 base）与 allow 放行的，在「角色」页编辑。种类与 owner 筛选放在地址里（?kind=&owner=）
 */
export default function Assets() {
  const [params, setParams] = useSearchParams()
  const kindParam = params.get('kind')
  const kind: AssetKind = isKind(kindParam) ? kindParam : 'skill'
  const owner = ownerFilterOf(params.get('owner'))
  const assets = useLoad(() => api.assets(), [])
  const agents = useAgents()
  const set = (patch: Record<string, string>) => setParams({ kind, owner, ...patch }, { replace: true })

  return (
    <Page title="AI 资产">
      <p className="text-sm text-muted">
        每项 AI 资产归平台或某个 agent（owner）。agent 对它能做什么按权限 <code className="font-mono">动作:种类:名字</code>{' '}
        算：动作是 use（用）与 write（改），owner 两者都有，其余来自它的角色（每个 agent 都有 base）与自己的 allow；admin
        能改全部。
      </p>
      <div className="max-w-xs">
        <SelectInput
          label="归属"
          value={owner}
          onChange={(v) => set({ owner: v })}
          options={[
            { id: 'all', label: '全部' },
            { id: 'platform', label: '平台' },
            ...(agents.data ?? []).map((a) => ({ id: a.id, label: a.name })),
          ]}
        />
      </div>
      <Tabs selectedKey={kind} onSelectionChange={(k: Key) => set({ kind: String(k) })} variant="secondary">
        <Tabs.ListContainer>
          <Tabs.List aria-label="资产种类">
            {KINDS.map((k) => (
              <Tabs.Tab key={k} id={k}>
                {KIND_LABEL[k]}
                {assets.data && `（${filterAssets(assets.data, k, owner).length}）`}
                <Tabs.Indicator />
              </Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs.ListContainer>
        {KINDS.map((k) => (
          <Tabs.Panel key={k} id={k} className="pt-3">
            <Loading state={assets}>
              {(all) => <KindTab kind={k} assets={all} owner={owner} />}
            </Loading>
          </Tabs.Panel>
        ))}
      </Tabs>
    </Page>
  )
}
