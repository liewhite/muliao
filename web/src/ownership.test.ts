import { describe, expect, it } from 'vitest'
import { asAgentId } from './api'
import { inheritedPublic, ownedBy } from './ownership'

const a = asAgentId('agent-a')
const b = asAgentId('agent-b')

const ALL = [
  { owner: null, name: 'search' },
  { owner: null, name: 'slack' },
  { owner: a, name: 'slack' },
  { owner: a, name: 'notes' },
  { owner: b, name: 'search' },
]
const nameOf = (x: { name: string }) => x.name

describe('ownedBy', () => {
  it('只给出公共资源', () => {
    expect(ownedBy(ALL, null).map(nameOf)).toEqual(['search', 'slack'])
  })

  it('只给出该 agent 的私有资源，看不到别人的', () => {
    expect(ownedBy(ALL, a).map(nameOf)).toEqual(['slack', 'notes'])
  })

  it('没有私有资源时为空', () => {
    expect(ownedBy(ALL, asAgentId('agent-c'))).toEqual([])
  })
})

describe('inheritedPublic', () => {
  it('列出全部公共资源，同名的标为被私有项遮蔽', () => {
    expect(inheritedPublic(ALL, a, nameOf)).toEqual([
      { item: { owner: null, name: 'search' }, shadowed: false },
      { item: { owner: null, name: 'slack' }, shadowed: true },
    ])
  })

  it('别的 agent 的同名私有资源不参与遮蔽判断', () => {
    expect(inheritedPublic(ALL, b, nameOf).map((i) => i.shadowed)).toEqual([true, false])
  })

  it('公共视角本身不继承任何东西', () => {
    expect(inheritedPublic(ALL, null, nameOf)).toEqual([])
  })
})
