import { describe, expect, it } from 'vitest'
import { liveToItems, type RunEvent } from './events'

const ev = (event: RunEvent['event'], text: string, tool_call_id: string | null = null): RunEvent =>
  event === 'text' || event === 'thinking'
    ? { type: 'run', session_id: 's', agent: 'a', event, text }
    : { type: 'run', session_id: 's', agent: 'a', event, text, tool_name: 'tool', tool_call_id: tool_call_id ?? '', attachments: [] }

describe('liveToItems', () => {
  it('joins consecutive thinking deltas into one thinking item before the answer', () => {
    const items = liveToItems([ev('thinking', '先'), ev('thinking', '想想'), ev('text', '答'), ev('text', '案')])
    expect(items.map((i) => [i.type, i.text])).toEqual([
      ['thinking', '先想想'],
      ['assistant', '答案'],
    ])
  })

  it('keeps alternating thinking, tool calls and answers as separate items in order', () => {
    const items = liveToItems([
      ev('thinking', 'a'),
      ev('tool_call', '{}', 'c1'),
      ev('tool_result', 'ok', 'c1'),
      ev('thinking', 'b'),
      ev('text', 'done'),
    ])
    expect(items.map((i) => [i.type, i.text])).toEqual([
      ['thinking', 'a'],
      ['tool_call', '{}'],
      ['tool_result', 'ok'],
      ['thinking', 'b'],
      ['assistant', 'done'],
    ])
  })
})
