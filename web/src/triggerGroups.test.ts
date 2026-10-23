import { describe, expect, it } from 'vitest'
import { asAgentId, type Agent, type Trigger } from './api'
import { groupByAgent } from './triggerGroups'

const a = asAgentId('a')
const b = asAgentId('b')
const gone = asAgentId('gone')

// 分组只看 id 与 agent_id，其余字段与本测试无关
const trigger = (id: number, agentId: typeof a) => ({ id, agent_id: agentId }) as Trigger
const agent = (id: typeof a) => ({ id }) as Agent

describe('groupByAgent', () => {
  it('组按 agent 列表的顺序排，组内保持原顺序', () => {
    const groups = groupByAgent([trigger(1, b), trigger(2, a), trigger(3, b)], [agent(a), agent(b)])
    expect(groups.map((g) => [g.agentId, g.triggers.map((t) => t.id)])).toEqual([
      [a, [2]],
      [b, [1, 3]],
    ])
  })

  it('agent 列表里没有的排在最后，列表未加载时仍能分组', () => {
    expect(groupByAgent([trigger(1, gone), trigger(2, a)], [agent(a)]).map((g) => g.agentId)).toEqual([a, gone])
    expect(groupByAgent([trigger(1, b), trigger(2, a)], undefined).map((g) => g.agentId)).toEqual([b, a])
  })

  it('没有 agent 的空组不出现', () => {
    expect(groupByAgent([], [agent(a)])).toEqual([])
  })
})
