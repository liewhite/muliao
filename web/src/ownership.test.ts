import { describe, expect, it } from 'vitest'
import { asAgentId } from './api'
import { splitView } from './ownership'

const a = asAgentId('agent-a')

// 接口给出的 a 视角：私有在前，公共在后
const VIEW_A = [
  { owner: a, name: 'slack' },
  { owner: null, name: 'search' },
  { owner: null, name: 'slack' },
]
const nameOf = (x: { name: string }) => x.name

describe('splitView', () => {
  it('私有视角：自己管理的是私有项，公共项是继承来的', () => {
    const { own, inherited } = splitView(VIEW_A, a)
    expect(own.map(nameOf)).toEqual(['slack'])
    expect(inherited.map(nameOf)).toEqual(['search', 'slack'])
  })

  it('公共视角：自己管理的就是公共项，不继承任何东西', () => {
    const publicView = VIEW_A.filter((x) => x.owner === null)
    expect(splitView(publicView, null)).toEqual({ own: publicView, inherited: [] })
  })
})
