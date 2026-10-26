import { Card, Tabs, type Key } from '@heroui/react'
import { useSearchParams } from 'react-router-dom'
import { api, asAgentId, type CatalogItem, type ItemKind } from '../api'
import { KIND_LABEL, KINDS, filterItems, isKind, type OwnerFilter } from '../rules'
import { ItemAccess } from './itemParts'
import { McpTab } from './Mcp'
import { ToolLine } from './Permissions'
import { SecretTab } from './Vault'
import { SkillTab } from './Skills'
import { EmptyHint, Loading, Page, SelectInput, useAgents, useLoad } from '../ui'

/** 内置工具：归平台、代码里定义，不能改；只看它被哪些角色收录 */
function ToolTab({ items }: { items: CatalogItem[] }) {
  const tools = useLoad(() => api.tools(), [])
  return (
    <Loading state={tools}>
      {(all) => {
        const byName = new Map(all.map((t) => [t.name, t]))
        return items.length === 0 ? (
          <EmptyHint>没有工具</EmptyHint>
        ) : (
          <div className="flex flex-col gap-2">
            {items.map((i) => {
              const tool = byName.get(i.name)
              if (!tool) throw new Error(`tool ${i.name} is in /items but not in /tools`)
              return (
                <Card key={i.name}>
                  <Card.Content className="flex flex-col gap-1.5 text-sm">
                    <ToolLine tool={tool} />
                    <ItemAccess item={i} />
                  </Card.Content>
                </Card>
              )
            })}
          </div>
        )
      }}
    </Loading>
  )
}

function KindTab({ kind, items, onChange }: { kind: ItemKind; items: CatalogItem[]; onChange: () => void }) {
  switch (kind) {
    case 'tool':
      return <ToolTab items={items} />
    case 'skill':
      return <SkillTab items={items} onChange={onChange} />
    case 'mcp':
      return <McpTab items={items} onChange={onChange} />
    case 'secret':
      return <SecretTab items={items} onChange={onChange} />
  }
}

function ownerFilterOf(value: string | null): OwnerFilter {
  if (value === null || value === 'all') return 'all'
  return value === 'platform' ? 'platform' : asAgentId(value)
}

/**
 * 条目池：全部工具、skill、MCP server、secret，不分公共私有。每项显示 owner（平台或某个 agent）与收录它的角色；
 * agent 能用的 = 它自己的 + 它的角色（含 base）收录的，在「角色」页编辑。种类与 owner 筛选放在地址里（?kind=&owner=）
 */
export default function Items() {
  const [params, setParams] = useSearchParams()
  const kindParam = params.get('kind')
  const kind: ItemKind = isKind(kindParam) ? kindParam : 'skill'
  const owner = ownerFilterOf(params.get('owner'))
  const items = useLoad(() => api.items(), [])
  const agents = useAgents()
  const set = (patch: Record<string, string>) => setParams({ kind, owner, ...patch }, { replace: true })

  return (
    <Page title="条目">
      <p className="text-sm text-muted">
        条目归平台或某个 agent（owner）：owner 与 admin 能改。agent 能用的是它自己的，加上它的角色（每个 agent 都有
        base）收录的。
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
          <Tabs.List aria-label="条目种类">
            {KINDS.map((k) => (
              <Tabs.Tab key={k} id={k}>
                {KIND_LABEL[k]}
                {items.data && `（${filterItems(items.data, k, owner).length}）`}
                <Tabs.Indicator />
              </Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs.ListContainer>
        {KINDS.map((k) => (
          <Tabs.Panel key={k} id={k} className="pt-3">
            <Loading state={items}>
              {(all) => <KindTab kind={k} items={filterItems(all, k, owner)} onChange={() => void items.reload()} />}
            </Loading>
          </Tabs.Panel>
        ))}
      </Tabs>
    </Page>
  )
}
