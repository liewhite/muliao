import { describe, expect, it } from 'vitest'
import { asAgentId } from './api'
import { FILTERS, ORIGIN_LABEL, groupByAgent, parseFilter, splitActive } from './sessionGroups'

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

describe('FILTERS', () => {
  it('除「全部」外，每个会话来源恰好属于一个分类', () => {
    expect(FILTERS.all.origins).toBeNull()
    const covered = Object.values(FILTERS).flatMap((f) => f.origins ?? [])
    expect([...covered].sort()).toEqual(Object.keys(ORIGIN_LABEL).sort())
  })
})

describe('parseFilter', () => {
  it('记住的分类原样取回，没记过或不认识的值回到「对话」', () => {
    expect(parseFilter('issue')).toBe('issue')
    expect(parseFilter('auto')).toBe('auto')
    expect(parseFilter('all')).toBe('all')
    expect(parseFilter(null)).toBe('chat')
    expect(parseFilter('inbox')).toBe('chat')
    expect(parseFilter('toString')).toBe('chat')
  })
})
