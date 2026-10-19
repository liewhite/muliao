import { describe, expect, it } from 'vitest'
import { liveToItems, mergeLive, type RunEvent } from './events'

const ev = (event: RunEvent['event'], text: string, tool_call_id: string | null = null): RunEvent =>
  event === 'text' || event === 'thinking'
    ? { type: 'run', session_id: 's', agent: 'a', event, text, run_id: 'r', seq: 0 }
    : {
        type: 'run',
        session_id: 's',
        agent: 'a',
        event,
        text,
        tool_name: 'tool',
        tool_call_id: tool_call_id ?? '',
        attachments: [],
        run_id: 'r',
        seq: 0,
      }

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

describe('mergeLive', () => {
  const at = (run_id: string, seq: number, text = `${run_id}${seq}`): RunEvent => ({
    type: 'run',
    session_id: 's',
    agent: 'a',
    event: 'text',
    text,
    run_id,
    seq,
  })

  it('shows the turn from its start when the page opens mid-turn, without duplicates', () => {
    // 订阅后先收到了 seq 3、4，快照里有 0..3
    const merged = mergeLive({ run_id: 'r', events: [at('r', 0), at('r', 1), at('r', 2), at('r', 3)] }, [at('r', 3), at('r', 4)])
    expect(merged.events.map((e) => e.seq)).toEqual([0, 1, 2, 3, 4])
    expect(merged.gap).toBe(false)
  })

  it('reports a gap between the snapshot and the first event received over SSE', () => {
    // 快照到 seq 1，订阅建立时已经到了 seq 4：2、3 两边都没有
    const merged = mergeLive({ run_id: 'r', events: [at('r', 0), at('r', 1)] }, [at('r', 4), at('r', 5)])
    expect(merged.gap).toBe(true)
    expect(mergeLive({ run_id: 'r', events: [] }, [at('r', 0)]).gap).toBe(false)
    expect(mergeLive({ run_id: 'r', events: [] }, [at('r', 2)]).gap).toBe(true)
  })

  it('drops events of another run and keeps received events when there is no snapshot', () => {
    expect(mergeLive({ run_id: 'new', events: [at('new', 0)] }, [at('old', 7)]).events.map((e) => e.run_id)).toEqual(['new'])
    expect(mergeLive(null, [at('r', 0)]).events).toHaveLength(1)
  })
})
