import { describe, expect, it } from 'vitest'
import { conflictingFields, editedFields, followed } from './drafts'
import { coalesced } from './events'

describe('drafts', () => {
  const opened = { name: 'a', description: 'd', roles: ['x'] }

  it('submits only the fields the user changed', () => {
    expect(editedFields(opened, { ...opened, description: 'mine' })).toEqual(['description'])
    expect(editedFields(opened, { ...opened, roles: ['x'] })).toEqual([])
  })

  it('follows the server on untouched fields and keeps the edited ones', () => {
    const draft = { ...opened, description: 'mine' }
    const latest = { name: 'renamed by agent', description: 'theirs', roles: ['y'] }
    const next = followed(opened, draft, latest)
    expect(next.draft).toEqual({ name: 'renamed by agent', description: 'mine', roles: ['y'] })
    // 改过的字段基线不动：服务端的新值与基线不同，就是冲突
    expect(next.base).toEqual({ name: 'renamed by agent', description: 'd', roles: ['y'] })
    expect(conflictingFields(next.base, next.draft, latest)).toEqual(['description'])
  })

  it('takes a server value equal to the draft as the new base, not a conflict', () => {
    // 多半是自己刚保存的：改过的字段等于服务端的新值
    const draft = { ...opened, description: 'mine' }
    const latest = { ...opened, description: 'mine' }
    expect(conflictingFields(opened, draft, latest)).toEqual([])
    expect(followed(opened, draft, latest)).toEqual({ base: latest, draft: latest })
  })

  it('has no conflict while the server still holds the base', () => {
    expect(conflictingFields(opened, { ...opened, description: 'mine' }, opened)).toEqual([])
  })
})

describe('coalesced', () => {
  it('runs at most one more reload after the one in flight', async () => {
    let calls = 0
    let finish = () => {}
    const run = coalesced(
      () =>
        new Promise<void>((resolve) => {
          calls++
          finish = resolve
        }),
    )
    run()
    run()
    run()
    expect(calls).toBe(1)
    finish()
    await new Promise((r) => setTimeout(r, 0))
    expect(calls).toBe(2)
    finish()
    await new Promise((r) => setTimeout(r, 0))
    expect(calls).toBe(2)
  })
})
