import { describe, expect, it } from 'vitest'
import { asAgentId, type CatalogItem } from './api'
import { filterItems, ruleCandidates, splitRule } from './rules'

const a = asAgentId('agent-a')
const item = (kind: CatalogItem['kind'], name: string, owner: CatalogItem['owner']): CatalogItem => ({
  kind,
  name,
  owner,
  description: '',
  updated_at: null,
  roles: [],
})
const ITEMS = [item('tool', 'run_shell', null), item('skill', 'relay-host', null), item('skill', 'mine', a)]

describe('splitRule', () => {
  it('名字里的冒号属于名字', () => {
    expect(splitRule('secret:A:B')).toEqual({ kind: 'secret', name: 'A:B' })
  })

  it('不是条目的种类时报错', () => {
    expect(() => splitRule('agent:x')).toThrow()
  })
})

describe('ruleCandidates', () => {
  it('每种的通配符在前，然后是全部条目', () => {
    expect(ruleCandidates(ITEMS)).toEqual([
      'tool:*',
      'skill:*',
      'mcp:*',
      'secret:*',
      'tool:run_shell',
      'skill:relay-host',
      'skill:mine',
    ])
  })
})

describe('filterItems', () => {
  it('按种类与 owner 筛选', () => {
    const names = (owner: Parameters<typeof filterItems>[2]) => filterItems(ITEMS, 'skill', owner).map((i) => i.name)
    expect(names('all')).toEqual(['relay-host', 'mine'])
    expect(names('platform')).toEqual(['relay-host'])
    expect(names(a)).toEqual(['mine'])
  })
})
