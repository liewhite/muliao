import { describe, expect, it } from 'vitest'
import type { AskView, Item } from './api'
import { foldCheckIns, groupSteps, parseSource, stepAttachments, stepThreads, summarizeSteps, toBlocks, unlinkedThreads, type Step } from './blocks'

const item = (type: Item['type'], text: string, tool_call_id: string | null = null, at: string | null = null): Item => ({
  at,
  type,
  text,
  tool_name: tool_call_id ? 'run_shell' : null,
  check_in: false,
  tool_call_id,
  message_id: null,
  attachments: [],
  images: [],
  threads: [],
  ask: null,
})

describe('groupSteps', () => {
  it('folds consecutive thinking and tool calls into one group and keeps narration outside', () => {
    const views = groupSteps(
      toBlocks([
        item('user', '分析 ETH'),
        item('thinking', '先测网络'),
        item('assistant', '我来拉取实时数据'),
        item('tool_call', '{}', 'a'),
        item('tool_result', 'ok', 'a'),
        item('tool_call', '{}', 'b'),
        item('tool_result', 'ok', 'b'),
        item('assistant', '网络可用'),
        item('thinking', '拉 K 线'),
        item('tool_call', '{}', 'c'),
      ]),
    )
    expect(views.map((v) => (v.kind === 'process' ? `process:${v.steps.length}` : v.kind))).toEqual([
      'user',
      'process:1',
      'assistant',
      'process:2',
      'assistant',
      'process:2',
    ])
  })
})

describe('stepAttachments', () => {
  it('collects files attached by tool results, in order, for display outside the folded group', () => {
    const chart = { url: '/api/files/1/chart.png', name: 'chart.png', media_type: 'image/png', inline: true }
    const report = { url: '/api/files/2/r.md', name: 'r.md', media_type: 'text/markdown', inline: false }
    const views = groupSteps(
      toBlocks([
        item('tool_call', '{}', 'a'),
        { ...item('tool_result', 'shared', 'a'), attachments: [chart] },
        item('tool_call', '{}', 'b'),
        { ...item('tool_result', 'shared', 'b'), attachments: [report] },
      ]),
    )
    expect(views).toHaveLength(1)
    const group = views[0]
    expect(group.kind === 'process' && stepAttachments(group.steps)).toEqual([chart, report])
  })

  it('keeps images a tool returned to the model inside the call, not with the files for the user', () => {
    const shot = { url: '/api/files/3/shot.png', name: 'shot.png', media_type: 'image/png', inline: true }
    const views = groupSteps(toBlocks([item('tool_call', '{}', 'a'), { ...item('tool_result', 'page', 'a'), images: [shot] }]))
    const group = views[0]
    expect(group.kind === 'process' && stepAttachments(group.steps)).toEqual([])
    expect(group.kind === 'process' && group.steps[0].kind === 'tool' && group.steps[0].tool.images).toEqual([shot])
  })
})

describe('streaming stability', () => {
  // 渲染按下标作 key：流式追加（含后到的工具结果回写）时，已有视图的位置与组的起点不能变，否则用户展开的组会被重新挂载而折叠
  it('only ever grows views at the end as items stream in', () => {
    const items = [
      item('thinking', 'a'),
      item('tool_call', '{}', 'c1'),
      item('assistant', '说明'),
      item('tool_call', '{}', 'c2'),
      item('tool_result', 'ok', 'c1'),
      item('tool_error', 'orphan'),
      item('tool_result', 'ok', 'c2'),
      item('assistant', '结论'),
    ]
    const shape = (n: number) =>
      groupSteps(toBlocks(items.slice(0, n))).map((v) => (v.kind === 'process' ? 'process' : v.kind))
    for (let n = 1; n < items.length; n++) {
      const before = shape(n)
      expect(shape(n + 1).slice(0, before.length)).toEqual(before)
    }
    // 找不到对应调用的错误单独成步，并进所在的组
    const last = groupSteps(toBlocks(items))
    expect(last.map((v) => (v.kind === 'process' ? `process:${v.steps.length}` : v.kind))).toEqual([
      'process:2',
      'assistant',
      'process:2',
      'assistant',
    ])
  })
})

describe('toBlocks times', () => {
  it('takes the start from the call and the end from its result', () => {
    const [block] = toBlocks([
      item('tool_call', '{}', 'a', '2026-10-22T05:00:00Z'),
      item('tool_result', 'ok', 'a', '2026-10-22T05:00:04Z'),
    ])
    expect(block.kind === 'tool' && [block.tool.startedAt, block.tool.finishedAt]).toEqual([
      '2026-10-22T05:00:00Z',
      '2026-10-22T05:00:04Z',
    ])
  })
})

describe('summarizeSteps', () => {
  const tool = (result: string | null, rejected = false): Step => ({
    kind: 'tool',
    tool: { name: 'run_shell', args: '', result, rejected, attachments: [], images: [], threads: [], startedAt: null, finishedAt: null },
  })

  it('counts tool calls, mentions thinking, and reports the running tool and calls returned to the model', () => {
    expect(summarizeSteps([{ kind: 'thinking', text: 'x' }, tool('ok'), tool('err', true), tool(null)], true)).toEqual({
      label: '思考 · 3 次工具调用',
      running: 'run_shell',
      unfinished: 0,
      rejected: 1,
    })
    expect(summarizeSteps([tool('ok')], true)).toEqual({ label: '1 次工具调用', running: null, unfinished: 0, rejected: 0 })
    expect(summarizeSteps([{ kind: 'thinking', text: 'x' }], true).label).toBe('思考')
  })

  it('reports a call without a result in history as unfinished, not running', () => {
    expect(summarizeSteps([tool('ok'), tool(null)], false)).toEqual({
      label: '2 次工具调用',
      running: null,
      unfinished: 1,
      rejected: 0,
    })
  })
})

describe('ask_user blocks', () => {
  const state: AskView = { status: 'answered', questions: [], answers: [], note: null }
  const call = (args: string, ask: AskView | null = state): Item => ({ ...item('tool_call', args, 'q1'), tool_name: 'ask_user', ask })
  const result = (type: Item['type'], text: string): Item => ({ ...item(type, text, 'q1'), tool_name: 'ask_user' })

  it('keeps questions outside the step groups as their own interaction', () => {
    const views = groupSteps(toBlocks([item('tool_call', '{}', 'a'), call('{}'), item('tool_call', '{}', 'b')]))
    expect(views.map((v) => v.kind)).toEqual(['process', 'ask', 'process'])
  })

  it('takes the state from the call and does not show the result as a separate block', () => {
    const blocks = toBlocks([call('{}'), result('tool_result', '{"answered":true,"answers":[],"note":null}')])
    expect(blocks).toEqual([{ kind: 'ask', ask: { callId: 'q1', ask: state } }])
  })

  it('shows a rejected call, which has no ask state, as a rejected tool step', () => {
    const rejected = toBlocks([call('{"questions": []}', null), result('tool_error', 'too short')])
    expect(rejected.map((b) => (b.kind === 'tool' ? [b.kind, b.tool.rejected] : [b.kind]))).toEqual([['tool', true]])
  })
})

describe('stopped runs', () => {
  it('marks where the user stopped, outside the step group', () => {
    const views = groupSteps(toBlocks([item('tool_call', '{}', 'a'), item('tool_result', 'stopped', 'a'), item('stopped', '')]))
    expect(views.map((v) => v.kind)).toEqual(['process', 'stopped'])
  })
})

describe('stepThreads', () => {
  it('collects the threads opened by delegation calls in a group, once each, in order', () => {
    const views = groupSteps(
      toBlocks([
        item('tool_call', '{}', 'a'),
        { ...item('tool_result', 'delegated', 'a'), threads: ['t1'] },
        item('tool_call', '{}', 'b'),
        { ...item('tool_result', 'answer', 'b'), threads: ['t2'] },
        item('tool_call', '{}', 'c'),
        { ...item('tool_result', 'again', 'c'), threads: ['t1'] },
      ]),
    )
    expect(views[0].kind === 'process' && stepThreads(views[0].steps)).toEqual(['t1', 't2'])
  })
})

describe('unlinkedThreads', () => {
  it('lists only threads no item points at', () => {
    const items = [{ ...item('tool_result', 'x', 'a'), threads: ['t1'] }]
    expect(unlinkedThreads(items, [{ id: 't1' }, { id: 't2' }])).toEqual([{ id: 't2' }])
  })
})

describe('session events', () => {
  it('become their own block with the source, not part of a user bubble', () => {
    const event: Item = { ...item('event', 'job 1 finished'), tool_name: 'job:1' }
    expect(toBlocks([event, item('user', 'hi')])).toEqual([
      { kind: 'event', source: 'job:1', text: 'job 1 finished', checkIn: false, at: null },
      { kind: 'user', text: 'hi', attachments: [] },
    ])
  })
})

describe('foldCheckIns', () => {
  const check = (source: string, text: string, at: string): Item => ({ ...item('event', text, null, at), tool_name: source, check_in: true })
  const ending = (source: string, text: string): Item => ({ ...item('event', text), tool_name: source })
  const fold = (items: Item[]) => foldCheckIns(groupSteps(toBlocks(items)))

  it('folds consecutive check-ins of a source with the filler replies between them into one line', () => {
    const views = fold([
      check('thread:t1', 'still working (1 min)', '10:01'),
      item('assistant', '…'),
      check('thread:t1', 'still working (2 min)', '10:02'),
      item('assistant', ' ... '),
      check('thread:t1', 'still working (3 min)', '10:03'),
      ending('thread:t1', 'finished your task'),
      item('assistant', '它做完了'),
    ])
    expect(views).toEqual([
      {
        kind: 'checks',
        source: 'thread:t1',
        checks: [
          { text: 'still working (1 min)', at: '10:01' },
          { text: 'still working (2 min)', at: '10:02' },
          { text: 'still working (3 min)', at: '10:03' },
        ],
      },
      { kind: 'event', source: 'thread:t1', text: 'finished your task', checkIn: false, at: null },
      { kind: 'assistant', text: '它做完了' },
    ])
  })

  it('keeps interleaved sources apart and breaks the run on anything that is not a filler', () => {
    const views = fold([
      check('job:3', 'running', '1'),
      check('thread:t1', 'working', '2'),
      item('assistant', '…'),
      check('job:3', 'running', '3'),
      item('assistant', '日志里有报错，我去看看'),
      check('job:3', 'running', '4'),
    ])
    expect(views.map((v) => (v.kind === 'checks' ? `${v.source}×${v.checks.length}` : v.kind))).toEqual([
      'job:3×2',
      'thread:t1×1',
      'assistant',
      'job:3×1',
    ])
  })

  it('leaves a filler reply that does not follow a check-in alone', () => {
    expect(fold([item('user', 'hi'), item('assistant', '…')]).map((v) => v.kind)).toEqual(['user', 'assistant'])
  })
})

describe('parseSource', () => {
  it('reads threads and background jobs, anything else stays as is', () => {
    expect(parseSource('thread:abc-1')).toEqual({ kind: 'thread', id: 'abc-1' })
    expect(parseSource('job:12')).toEqual({ kind: 'job', id: 12 })
    expect(parseSource('job:x')).toEqual({ kind: 'other', source: 'job:x' })
    expect(parseSource('webhook')).toEqual({ kind: 'other', source: 'webhook' })
  })
})
