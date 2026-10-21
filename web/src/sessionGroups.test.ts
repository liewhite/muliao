import { describe, expect, it } from 'vitest'
import { asAgentId } from './api'
import { groupByAgent, splitActive } from './sessionGroups'

const a = asAgentId('agent-a')
const b = asAgentId('agent-b')

describe('groupByAgent', () => {
  it('空列表没有分组', () => {
    expect(groupByAgent([])).toEqual([])
  })

  it('按首次出现排列分组，组内保持顺序并统计未读', () => {
    const list = [
      { id: 1, agent_id: b, unread: true },
      { id: 2, agent_id: a, unread: false },
      { id: 3, agent_id: b, unread: false },
      { id: 4, agent_id: a, unread: true },
      { id: 5, agent_id: b, unread: true },
    ]
    const groups = groupByAgent(list)
    expect(groups.map((g) => g.agentId)).toEqual([b, a])
    expect(groups[0].sessions.map((s) => s.id)).toEqual([1, 3, 5])
    expect(groups[0].unread).toBe(2)
    expect(groups[1].sessions.map((s) => s.id)).toEqual([2, 4])
    expect(groups[1].unread).toBe(1)
  })
})

describe('splitActive', () => {
  it('在运行与在等待的都分出来置顶，两部分各自保持顺序', () => {
    const list = [
      { id: 1, state: 'running' as const },
      { id: 2, state: 'idle' as const },
      { id: 3, state: 'waiting' as const },
      { id: 4, state: 'idle' as const },
    ]
    const { active, rest } = splitActive(list)
    expect(active.map((s) => s.id)).toEqual([1, 3])
    expect(rest.map((s) => s.id)).toEqual([2, 4])
  })
})
