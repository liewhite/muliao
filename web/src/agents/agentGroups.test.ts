import { describe, expect, it } from 'vitest'
import { agentModel, groupAgents, searchAgents, selectableAgents } from './agentGroups'
import { asAgentId, type Agent } from '../api'

function agent(name: string, group: string | null = null, archived = false, description = ''): Agent {
  return {
    id: asAgentId(name),
    name,
    config: { description, model: { provider: 'p', model: 'm' }, thinking: 'default', group },
    roles: [],
    allow: [],
    deny: [],
    created_at: '2026-10-26T00:00:00Z',
    is_default: false,
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

describe('searchAgents', () => {
  const list = [agent('Slack-Hub', null, false, 'Slack 消息分发'), agent('trader', 'zhu', false, '量化交易'), agent('ops', 'zhu')]

  it('matches name or description case-insensitively, keeping order', () => {
    expect(searchAgents(list, 'slack').map((a) => a.name)).toEqual(['Slack-Hub'])
    expect(searchAgents(list, ' 交易 ').map((a) => a.name)).toEqual(['trader'])
    expect(searchAgents(list, 'A').map((a) => a.name)).toEqual(['Slack-Hub', 'trader'])
  })

  it('returns nothing for a blank query or no match', () => {
    expect(searchAgents(list, '  ')).toEqual([])
    expect(searchAgents(list, 'zzz')).toEqual([])
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

describe('agentModel', () => {
  it('gives the configured model, or nothing when the list, the agent or its config is missing', () => {
    const list = [agent('a'), { ...agent('b'), config: null }]
    expect(agentModel(list, asAgentId('a'))).toBe('m')
    expect(agentModel(list, asAgentId('b'))).toBeUndefined()
    expect(agentModel(list, asAgentId('zzz'))).toBeUndefined()
    expect(agentModel(undefined, asAgentId('a'))).toBeUndefined()
  })
})
