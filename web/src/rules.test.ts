import { describe, expect, it } from 'vitest'
import { asAgentId, type CatalogAsset } from './api'
import { filterAssets } from './rules'

const a = asAgentId('agent-a')
const common = (name: string, owner: CatalogAsset['owner']) => ({ name, owner, roles: [], shared: false, deletable: true, transferable: true })
const file = (path: string) => ({ description: '', updated_at: '2026-10-27T00:00:00Z', path })
const ASSETS: CatalogAsset[] = [
  { kind: 'tool', access: 'write', admin: false, ...common('run_shell', null) },
  { kind: 'skill', file: file('/a/platform/skills/relay-host'), ...common('relay-host', null) },
  { kind: 'skill', file: file('/a/agent-a/skills/mine'), ...common('mine', a) },
]

describe('filterAssets', () => {
  it('按种类与 owner 筛选', () => {
    const names = (owner: Parameters<typeof filterAssets>[2]) => filterAssets(ASSETS, 'skill', owner).map((i) => i.name)
    expect(names('all')).toEqual(['relay-host', 'mine'])
    expect(names('platform')).toEqual(['relay-host'])
    expect(names(a)).toEqual(['mine'])
  })
})
