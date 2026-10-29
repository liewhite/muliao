import { describe, expect, it } from 'vitest'
import type { AskView, Item } from './api'
import { foldCheckIns, groupSteps, stepAttachments, stepThreads, summarizeSteps, toBlocks, unlinkedThreads, type Step } from './blocks'

const item = (type: Item['type'], text: string, tool_call_id: string | null = null, at: string | null = null): Item => ({
  at,
  type,
  text,
  tool_name: tool_call_id ? 'run_shell' : null,
  source: null,
  check_in: false,
  tool_call_id,
  message_id: null,
  attachments: [],
  images: [],
  threads: [],
  ask: null,
  view: null,
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

  // #299：thread 与文件显示在组下方，之后的调用不能折进它上面的组
  it('starts a new group after the steps whose threads or files are shown below the group', () => {
    const delegate = (id: string, thread: string): Item => ({ ...item('tool_call', '{}', id), threads: [thread] })
    const report = { url: '/api/files/2/r.md', name: 'r.md', media_type: 'text/markdown', inline: false }
    const views = groupSteps(
      toBlocks([
        item('tool_call', '{}', 'a'),
        item('tool_result', 'ok', 'a'),
        delegate('b', 't1'),
        delegate('c', 't2'),
        item('tool_result', 'answer 1', 'b'),
        item('tool_result', 'answer 2', 'c'),
        item('thinking', '看回答'),
        item('tool_call', '{}', 'd'),
        { ...item('tool_result', 'shared', 'd'), attachments: [report] },
        item('tool_call', '{}', 'e'),
      ]),
    )
    expect(views.map((v) => (v.kind === 'process' ? stepThreads(v.steps).join(',') + `:${v.steps.length}` : v.kind))).toEqual(
      ['t1,t2:3', ':2', ':1'],
    )
    expect(views.map((v) => v.kind === 'process' && stepAttachments(v.steps).length)).toEqual([0, 1, 0])
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

  it('takes the call view from the call item and the result view from its result', () => {
    const cmd = { kind: 'terminal' as const, command: 'ls', output: null, status: null, failed: false }
    const out = { kind: 'terminal' as const, command: null, output: 'a', status: null, failed: false }
    const [block] = toBlocks([{ ...item('tool_call', '{}', 'a'), view: [cmd] }, { ...item('tool_result', 'a', 'a'), view: [out] }])
    expect(block.kind === 'tool' && [block.tool.callView, block.tool.resultView]).toEqual([[cmd], [out]])
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
    // #299：结果带来的文件把组切开，新组也只出现在末尾
    const report = { url: '/api/files/2/r.md', name: 'r.md', media_type: 'text/markdown', inline: false }
    const withFiles = [
      item('tool_call', '{}', 'f1'),
      item('tool_call', '{}', 'f2'),
      { ...item('tool_result', 'shared', 'f1'), attachments: [report] },
      item('tool_result', 'ok', 'f2'),
      item('thinking', 'b'),
      item('tool_call', '{}', 'f3'),
    ]
    const fileShape = (n: number) => groupSteps(toBlocks(withFiles.slice(0, n))).map((v) => (v.kind === 'process' ? 'process' : v.kind))
    for (let n = 1; n < withFiles.length; n++) {
      const before = fileShape(n)
      expect(fileShape(n + 1).slice(0, before.length)).toEqual(before)
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
    tool: { name: 'run_shell', args: '', result, rejected, attachments: [], images: [], threads: [], startedAt: null, finishedAt: null, callView: null, resultView: null },
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
        { ...item('tool_call', '{}', 'a'), threads: ['t1'] },
        item('tool_result', 'delegated', 'a'),
        { ...item('tool_call', '{}', 'b'), threads: ['t2'] },
        item('tool_result', 'answer', 'b'),
        { ...item('tool_call', '{}', 'c'), threads: ['t1'] },
        item('tool_result', 'again', 'c'),
      ]),
    )
    expect(views[0].kind === 'process' && stepThreads(views[0].steps)).toEqual(['t1', 't2'])
  })

  it('includes the thread of a call still waiting for its answer, and keeps it once the result arrives', () => {
    const call = { ...item('tool_call', '{}', 'a'), threads: ['t1'] }
    for (const items of [[call], [call, item('tool_result', 'stopped', 'a')]]) {
      const views = groupSteps(toBlocks(items))
      expect(views[0].kind === 'process' && stepThreads(views[0].steps)).toEqual(['t1'])
    }
  })
})

describe('unlinkedThreads', () => {
  it('lists only threads no item points at', () => {
    const items = [{ ...item('tool_call', '{}', 'a'), threads: ['t1'] }]
    expect(unlinkedThreads(items, [{ id: 't1' }, { id: 't2' }])).toEqual([{ id: 't2' }])
  })
})

const JOB_1 = { kind: 'job', raw: 'job:1', job_id: 1 } as const

describe('session events', () => {
  it('become their own block with the source, not part of a user bubble', () => {
    const event: Item = { ...item('event', 'job 1 finished'), source: JOB_1 }
    expect(toBlocks([event, item('user', 'hi')])).toEqual([
      { kind: 'event', source: JOB_1, text: 'job 1 finished', checkIn: false, at: null },
      { kind: 'user', text: 'hi', attachments: [] },
    ])
  })
})

describe('foldCheckIns', () => {
  const check = (source: string, text: string, at: string): Item => ({ ...item('event', text, null, at), source: { kind: 'other', raw: source }, check_in: true })
  const ending = (source: string, text: string): Item => ({ ...item('event', text), source: { kind: 'other', raw: source } })
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
        source: { kind: 'other', raw: 'thread:t1' },
        checks: [
          { text: 'still working (1 min)', at: '10:01' },
          { text: 'still working (2 min)', at: '10:02' },
          { text: 'still working (3 min)', at: '10:03' },
        ],
      },
      { kind: 'event', source: { kind: 'other', raw: 'thread:t1' }, text: 'finished your task', checkIn: false, at: null },
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
    expect(views.map((v) => (v.kind === 'checks' ? `${v.source.raw}×${v.checks.length}` : v.kind))).toEqual([
      'job:3×2',
      'thread:t1×1',
      'assistant',
      'job:3×1',
    ])
  })

  it('folds thinking only when it leads to a filler reply', () => {
    const views = fold([
      check('job:3', 'running', '1'),
      item('thinking', '还在跑，等着'),
      item('assistant', '…'),
      check('job:3', 'running', '2'),
      item('thinking', '输出卡住了'),
      item('assistant', '我去看看日志'),
    ])
    expect(views.map((v) => (v.kind === 'checks' ? `${v.source.raw}×${v.checks.length}` : v.kind))).toEqual([
      'job:3×2',
      'process',
      'assistant',
    ])
  })

  it('leaves a filler reply that does not follow a check-in alone', () => {
    expect(fold([item('user', 'hi'), item('assistant', '…')]).map((v) => v.kind)).toEqual(['user', 'assistant'])
  })
})
