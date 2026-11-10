import { describe, expect, it } from 'vitest'
import { changedFields, rebased } from './drafts'
import { coalesced } from './events'

describe('drafts', () => {
  const opened = { name: 'a', description: 'd', roles: ['x'] }

  it('submits only the fields the user changed', () => {
    expect(changedFields(opened, { ...opened, description: 'mine' })).toEqual({ description: 'mine' })
    expect(changedFields(opened, { ...opened, roles: ['x'] })).toEqual({})
  })

  it('follows the server on untouched fields and keeps the edited ones', () => {
    const draft = { ...opened, description: 'mine' }
    const latest = { name: 'renamed by agent', description: 'theirs', roles: ['y'] }
    expect(rebased(opened, draft, latest)).toEqual({ name: 'renamed by agent', description: 'mine', roles: ['y'] })
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
