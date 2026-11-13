import { describe, expect, it } from 'vitest'
import { asAgentId, type CatalogAsset } from './api'
import { filterAssets, ruleCandidates } from './rules'

const a = asAgentId('agent-a')
const common = (name: string, owner: CatalogAsset['owner']) => ({ name, owner, description: '', roles: [], shared: false, deletable: true })
const AT = '2026-10-27T00:00:00Z'
const ASSETS: CatalogAsset[] = [
  { kind: 'tool', access: 'write', admin: false, updated_at: null, path: null, ...common('run_shell', null) },
  { kind: 'skill', updated_at: AT, path: '/a/platform/skills/relay-host', ...common('relay-host', null) },
  { kind: 'skill', updated_at: AT, path: '/a/agent-a/skills/mine', ...common('mine', a) },
]

describe('ruleCandidates', () => {
  it('每种的通配符在前，然后是全部资产（存在库里的还有 use + write）', () => {
    expect(ruleCandidates(ASSETS)).toEqual([
      'use:tool:*',
      'use:skill:*',
      'use:mcp:*',
      'use:secret:*',
      'use:memory:*',
      'use:tool:run_shell',
      'use:skill:relay-host',
      '*:skill:relay-host',
      'use:skill:mine',
      '*:skill:mine',
    ])
  })
})

describe('filterAssets', () => {
  it('按种类与 owner 筛选', () => {
    const names = (owner: Parameters<typeof filterAssets>[2]) => filterAssets(ASSETS, 'skill', owner).map((i) => i.name)
    expect(names('all')).toEqual(['relay-host', 'mine'])
    expect(names('platform')).toEqual(['relay-host'])
    expect(names(a)).toEqual(['mine'])
  })
})
