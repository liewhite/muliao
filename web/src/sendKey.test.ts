import { describe, expect, it } from 'vitest'
import { isSendShortcut } from './sendKey'

const key = (k: string, mods: { meta?: boolean; ctrl?: boolean; shift?: boolean; composing?: boolean } = {}) => ({
  key: k,
  metaKey: mods.meta ?? false,
  ctrlKey: mods.ctrl ?? false,
  shiftKey: mods.shift ?? false,
  nativeEvent: { isComposing: mods.composing ?? false },
})

describe('isSendShortcut', () => {
  it('sends on Cmd+Enter or Ctrl+Enter', () => {
    expect(isSendShortcut(key('Enter', { meta: true }))).toBe(true)
    expect(isSendShortcut(key('Enter', { ctrl: true }))).toBe(true)
  })

  it('leaves plain and Shift+Enter as newline', () => {
    expect(isSendShortcut(key('Enter'))).toBe(false)
    expect(isSendShortcut(key('Enter', { shift: true }))).toBe(false)
  })

  it('ignores Enter while the IME is composing', () => {
    expect(isSendShortcut(key('Enter', { meta: true, composing: true }))).toBe(false)
  })

  it('ignores other keys', () => {
    expect(isSendShortcut(key('a', { meta: true }))).toBe(false)
  })
})
