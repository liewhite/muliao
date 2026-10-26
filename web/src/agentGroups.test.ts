import { describe, expect, it } from 'vitest'
import { groupAgents, selectableAgents } from './agentGroups'
import { asAgentId, type Agent } from './api'

function agent(name: string, group: string | null = null, archived = false): Agent {
  return {
    id: asAgentId(name),
    name,
    description: '',
    model: 'p:m',
    roles: [],
    allow: [],
    deny: [],
    thinking: 'default',
    created_at: '2026-10-26T00:00:00Z',
    is_default: false,
    group,
    archived_at: archived ? '2026-10-26T01:00:00Z' : null,
  }
}

describe('groupAgents', () => {
  it('puts ungrouped first, groups by name, archived apart whatever their group', () => {
    const list = [
      agent('a'),
      agent('b', 'zhu'),
      agent('c', 'biz'),
      agent('d', 'zhu', true),
      agent('e', null, true),
      agent('f', 'zhu'),
    ]
    const g = groupAgents(list)
    expect(g.ungrouped.map((a) => a.name)).toEqual(['a'])
    expect(g.groups.map((x) => [x.name, x.agents.map((a) => a.name)])).toEqual([
      ['biz', ['c']],
      ['zhu', ['b', 'f']],
    ])
    expect(g.archived.map((a) => a.name)).toEqual(['d', 'e'])
  })
})

describe('selectableAgents', () => {
  it('leaves out archived agents except the one already chosen', () => {
    const list = [agent('a'), agent('b', null, true), agent('c', null, true)]
    expect(selectableAgents(list).map((a) => a.name)).toEqual(['a'])
    expect(selectableAgents(list, 'b').map((a) => a.name)).toEqual(['a', 'b'])
    expect(selectableAgents(undefined)).toEqual([])
  })
})
