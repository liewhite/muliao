import { describe, expect, it } from 'vitest'
import { isOutdated, isStale, liveItems, mergeLive, needsReconnect, withoutCovered, type RunEvent } from './events'

const base = { type: 'run', session_id: 's', agent: 'a', run_id: 'r', seq: 0 } as const

const delta = (item_type: 'assistant' | 'thinking', text: string): RunEvent => ({ ...base, event: 'delta', item_type, text })

const tool = (type: 'tool_call' | 'tool_result', text: string, tool_call_id: string): RunEvent => ({
  ...base,
  event: 'item',
  item: { type, text, tool_name: 'tool', tool_call_id, message_id: null, attachments: [], threads: [], ask: null },
})

describe('liveItems', () => {
  it('joins consecutive thinking deltas into one thinking item before the answer', () => {
    const items = liveItems([delta('thinking', '先'), delta('thinking', '想想'), delta('assistant', '答'), delta('assistant', '案')])
    expect(items.map((i) => [i.type, i.text])).toEqual([
      ['thinking', '先想想'],
      ['assistant', '答案'],
    ])
  })

  it('keeps alternating thinking, tool items and answers as separate items in order', () => {
    const items = liveItems([
      delta('thinking', 'a'),
      tool('tool_call', '{}', 'c1'),
      tool('tool_result', 'ok', 'c1'),
      delta('thinking', 'b'),
      delta('assistant', 'done'),
    ])
    expect(items.map((i) => [i.type, i.text])).toEqual([
      ['thinking', 'a'],
      ['tool_call', '{}'],
      ['tool_result', 'ok'],
      ['thinking', 'b'],
      ['assistant', 'done'],
    ])
  })

  it('does not change the events it folds', () => {
    const events = [tool('tool_call', '{}', 'c1'), delta('assistant', 'x')]
    const before = structuredClone(events)
    liveItems([...events, delta('assistant', 'y')])
    expect(events).toEqual(before)
  })
})

describe('mergeLive', () => {
  const at = (run_id: string, seq: number, text = `${run_id}${seq}`): RunEvent => ({
    ...delta('assistant', text),
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

  it('drops received events the snapshot covers when the next one is in the same segment', () => {
    const snapshot = { run_id: 'r', events: [at('r', 0), at('r', 1)] }
    const kept = withoutCovered(snapshot, [at('old', 5), at('r', 1), at('r', 2)], at('r', 3))
    expect(kept.map((e) => [e.run_id, e.seq])).toEqual([['r', 2]])
  })

  it('keeps every received event of a newer segment until its snapshot arrives', () => {
    // 快照还是上一段 a，新一段 b 的事件陆续到达：都要留着，新快照可能早于其中一些生成
    const snapshot = { run_id: 'a', events: [at('a', 0)] }
    expect(withoutCovered(snapshot, [at('b', 0), at('b', 1)], at('b', 2))).toHaveLength(2)
    expect(withoutCovered(null, [at('r', 0)], at('r', 1))).toHaveLength(1)
  })
})

describe('isStale', () => {
  it('keeps a connection that heard anything within two heartbeats plus slack', () => {
    expect(isStale(0, 35_000, 15)).toBe(false)
  })
  it('gives up on a connection silent for longer, e.g. after the phone was in the background', () => {
    expect(isStale(0, 35_001, 15)).toBe(true)
    expect(isStale(0, 10 * 60_000, 15)).toBe(true)
  })
})

describe('mergeLive without a snapshot', () => {
  const at = (seq: number): RunEvent => ({ ...delta('assistant', 'x'), seq })
  it('keeps events that start at the beginning of the run', () => {
    expect(mergeLive(null, [at(0), at(1)]).gap).toBe(false)
  })
  it('reports a gap when the first events of the run were missed', () => {
    // 快照生成时这一轮还没开始，SSE 连上时已经开始了：开头的事件两边都没有
    expect(mergeLive(null, [at(3), at(4)]).gap).toBe(true)
  })
})

describe('needsReconnect', () => {
  it('rebuilds a connection the server closed, even before the first heartbeat', () => {
    // 第一次连接就收到 502（服务重启中）：EventSource 不再自己重连
    expect(needsReconnect(true, 0, 1, null)).toBe(true)
  })
  it('does not judge silence before knowing the heartbeat interval', () => {
    expect(needsReconnect(false, 0, 10 * 60_000, null)).toBe(false)
  })
  it('rebuilds a silent connection once the interval is known', () => {
    expect(needsReconnect(false, 0, 35_001, 15)).toBe(true)
    expect(needsReconnect(false, 0, 35_000, 15)).toBe(false)
  })
})

describe('isOutdated', () => {
  it('flags a page whose build differs from the one the server now serves', () => {
    expect(isOutdated('aaa', 'bbb')).toBe(true)
    expect(isOutdated('aaa', 'aaa')).toBe(false)
  })
  it('never flags when either side has no frontend build', () => {
    expect(isOutdated(null, 'bbb')).toBe(false)
    expect(isOutdated('aaa', null)).toBe(false)
  })
})
