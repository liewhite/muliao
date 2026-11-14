import { describe, expect, it } from 'vitest'
import { asAgentId, type SitePage } from '../api'
import { groupPagesByAgent } from './pageGroups'

const a = asAgentId('a')
const b = asAgentId('b')

// 分组只看 agent_id、name 与 modified，其余字段与本测试无关
const page = (agentId: typeof a, name: string, modified: string) => ({ agent_id: agentId, name, modified }) as SitePage

describe('groupPagesByAgent', () => {
  it('组内按更新时间倒序，组之间按组里最新页面的时间倒序', () => {
    const groups = groupPagesByAgent([
      page(a, 'a-old', '2026-11-01T00:00:00Z'),
      page(b, 'b-mid', '2026-11-05T00:00:00Z'),
      page(a, 'a-new', '2026-11-09T00:00:00Z'),
      page(b, 'b-old', '2026-11-02T00:00:00Z'),
    ])
    expect(groups.map((g) => [g.agentId, g.pages.map((p) => p.name)])).toEqual([
      [a, ['a-new', 'a-old']],
      [b, ['b-mid', 'b-old']],
    ])
  })

  it('按时间比较而不是按字符串：不同时区写法也排对', () => {
    const groups = groupPagesByAgent([
      page(a, 'utc-0900', '2026-11-09T09:00:00Z'),
      page(b, 'cst-1000', '2026-11-09T10:00:00+08:00'),
    ])
    expect(groups.map((g) => g.agentId)).toEqual([a, b])
  })

  it('没有页面时没有组', () => {
    expect(groupPagesByAgent([])).toEqual([])
  })
})
