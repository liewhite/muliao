import { describe, expect, it } from 'vitest'
import { draftState, editedFields, followed, replacedBy } from './drafts'
import { coalesced, debounced } from './events'

describe('drafts', () => {
  const opened = { name: 'a', description: 'd', roles: ['x'] }

  it('submits only the fields the user changed', () => {
    expect(editedFields(opened, { ...opened, description: 'mine' })).toEqual(['description'])
    expect(editedFields(opened, { ...opened, roles: ['x'] })).toEqual([])
  })

  it('follows the server until the user edits, then keeps the draft and its base', () => {
    const latest = { name: 'renamed by agent', description: 'theirs', roles: ['y'] }
    const clean = followed(draftState(opened), latest)
    expect([clean.base, clean.draft, clean.replaced]).toEqual([latest, latest, { name: 1, description: 1, roles: 1 }])
    const edited = { ...draftState(opened), draft: { ...opened, description: 'mine' } }
    expect(followed(edited, latest)).toBe(edited)
  })

  it('starts over from what was saved', () => {
    const s = { ...draftState(opened), draft: { ...opened, description: 'mine' } }
    const saved = { ...opened, description: 'mine' }
    const next = replacedBy(s, saved)
    expect([next.base, next.draft, next.replaced]).toEqual([saved, saved, {}])
  })
})

describe('debounced', () => {
  it('runs once after the wait for all calls in it, and not after cancel', async () => {
    let calls = 0
    const d = debounced(() => calls++, 5)
    d.run()
    d.run()
    await new Promise((r) => setTimeout(r, 20))
    expect(calls).toBe(1)
    d.run()
    d.cancel()
    await new Promise((r) => setTimeout(r, 20))
    expect(calls).toBe(1)
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
