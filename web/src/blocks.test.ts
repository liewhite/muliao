import { describe, expect, it } from 'vitest'
import { asSessionId, sourceKey, type AskView, type Item, type ItemSource } from './api'
import { foldCheckIns, groupSteps, stepAttachments, stepThreads, summarizeSteps, toBlocks, unlinkedThreads, type Step } from './blocks'

type Of<T extends Item['type']> = Extract<Item, { type: T }>
const user = (text: string): Item => ({ type: 'user', text, attachments: [] })
const thinking = (text: string): Item => ({ type: 'thinking', text })
const assistant = (text: string): Item => ({ type: 'assistant', text })
const stopped: Item = { type: 'stopped' }
const call = (tool_call_id: string, text = '{}', at: string | null = null): Of<'tool_call'> => ({
  type: 'tool_call',
  tool_name: 'run_shell',
  tool_call_id,
  text,
  threads: [],
  wait: null,
  view: null,
  at,
})
const result = (tool_call_id: string, text: string, at: string | null = null): Of<'tool_result'> => ({
  type: 'tool_result',
  tool_name: 'run_shell',
  tool_call_id,
  text,
  attachments: [],
  images: [],
  view: null,
  at,
})
const error = (tool_call_id: string, text: string): Of<'tool_error'> => ({ type: 'tool_error', tool_name: 'run_shell', tool_call_id, text, at: null })
const event = (source: Of<'event'>['source'], text: string, at = '10:00', check_in = false): Item => ({ type: 'event', source, text, at, check_in })

describe('groupSteps', () => {
  it('folds consecutive thinking and tool calls into one group and keeps narration outside', () => {
    const views = groupSteps(
      toBlocks([
        user('分析 ETH'),
        thinking('先测网络'),
        assistant('我来拉取实时数据'),
        call('a', '{}'),
        result('a', 'ok'),
        call('b', '{}'),
        result('b', 'ok'),
        assistant('网络可用'),
        thinking('拉 K 线'),
        call('c', '{}'),
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
    const delegate = (id: string, thread: string): Item => ({ ...call(id), threads: [asSessionId(thread)] })
    const report = { url: '/api/files/2/r.md', name: 'r.md', media_type: 'text/markdown', inline: false }
    const views = groupSteps(
      toBlocks([
        call('a', '{}'),
        result('a', 'ok'),
        delegate('b', 't1'),
        delegate('c', 't2'),
        result('b', 'answer 1'),
        result('c', 'answer 2'),
        thinking('看回答'),
        call('d', '{}'),
        { ...result('d', 'shared'), attachments: [report] },
        call('e', '{}'),
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
        call('a', '{}'),
        { ...result('a', 'shared'), attachments: [chart] },
        call('b', '{}'),
        { ...result('b', 'shared'), attachments: [report] },
      ]),
    )
    expect(views).toHaveLength(1)
    const group = views[0]
    expect(group.kind === 'process' && stepAttachments(group.steps)).toEqual([chart, report])
  })

  it('keeps images a tool returned to the model inside the call, not with the files for the user', () => {
    const shot = { url: '/api/files/3/shot.png', name: 'shot.png', media_type: 'image/png', inline: true }
    const views = groupSteps(toBlocks([call('a', '{}'), { ...result('a', 'page'), images: [shot] }]))
    const group = views[0]
    expect(group.kind === 'process' && stepAttachments(group.steps)).toEqual([])
    expect(group.kind === 'process' && group.steps[0].kind === 'tool' && group.steps[0].tool.images).toEqual([shot])
  })

  it('takes the call view from the call item and the result view from its result', () => {
    const cmd = { kind: 'terminal' as const, command: 'ls', output: null, status: null, failed: false }
    const out = { kind: 'terminal' as const, command: null, output: 'a', status: null, failed: false }
    const [block] = toBlocks([{ ...call('a', '{}'), view: [cmd] }, { ...result('a', 'a'), view: [out] }])
    expect(block.kind === 'tool' && [block.tool.callView, block.tool.resultView]).toEqual([[cmd], [out]])
  })
})

describe('streaming stability', () => {
  // 渲染按下标作 key：流式追加（含后到的工具结果回写）时，已有视图的位置与组的起点不能变，否则用户展开的组会被重新挂载而折叠
  it('only ever grows views at the end as items stream in', () => {
    const items = [
      thinking('a'),
      call('c1', '{}'),
      assistant('说明'),
      call('c2', '{}'),
      result('c1', 'ok'),
      error('z', 'orphan'),
      result('c2', 'ok'),
      assistant('结论'),
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
      call('f1', '{}'),
      call('f2', '{}'),
      { ...result('f1', 'shared'), attachments: [report] },
      result('f2', 'ok'),
      thinking('b'),
      call('f3', '{}'),
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
      call('a', '{}', '2026-10-22T05:00:00Z'),
      result('a', 'ok', '2026-10-22T05:00:04Z'),
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
  const state: AskView = { questions: [], state: { status: 'answered', answers: [] } }
  const ask = (args: string, wait: AskView | null = state): Item => ({ ...call('q1', args), tool_name: 'ask_user', wait: wait && { kind: 'ask', ...wait } })
  const answer = (text: string): Item => ({ ...result('q1', text), tool_name: 'ask_user' })
  const refused = (text: string): Item => ({ ...error('q1', text), tool_name: 'ask_user' })

  it('keeps questions outside the step groups as their own interaction', () => {
    const views = groupSteps(toBlocks([call('a', '{}'), ask('{}'), call('b', '{}')]))
    expect(views.map((v) => v.kind)).toEqual(['process', 'ask', 'process'])
  })

  it('takes the state from the call and does not show the result as a separate block', () => {
    const blocks = toBlocks([ask('{}'), answer('{"answered":true,"answers":[],"note":null}')])
    expect(blocks).toEqual([{ kind: 'ask', ask: { callId: 'q1', ask: { kind: 'ask', ...state } } }])
  })

  it('shows a rejected call, which has no ask state, as a rejected tool step', () => {
    const rejected = toBlocks([ask('{"questions": []}', null), refused('too short')])
    expect(rejected.map((b) => (b.kind === 'tool' ? [b.kind, b.tool.rejected] : [b.kind]))).toEqual([['tool', true]])
  })
})

describe('stopped runs', () => {
  it('marks where the user stopped, outside the step group', () => {
    const views = groupSteps(toBlocks([call('a', '{}'), result('a', 'stopped'), stopped]))
    expect(views.map((v) => v.kind)).toEqual(['process', 'stopped'])
  })
})

describe('stepThreads', () => {
  it('collects the threads opened by delegation calls in a group, once each, in order', () => {
    const views = groupSteps(
      toBlocks([
        { ...call('a', '{}'), threads: [asSessionId('t1')] },
        result('a', 'delegated'),
        { ...call('b', '{}'), threads: [asSessionId('t2')] },
        result('b', 'answer'),
        { ...call('c', '{}'), threads: [asSessionId('t1')] },
        result('c', 'again'),
      ]),
    )
    expect(views[0].kind === 'process' && stepThreads(views[0].steps)).toEqual(['t1', 't2'])
  })

  it('includes the thread of a call still waiting for its answer, and keeps it once the result arrives', () => {
    const opened = { ...call('a'), threads: [asSessionId('t1')] }
    for (const items of [[opened], [opened, result('a', 'stopped')]]) {
      const views = groupSteps(toBlocks(items))
      expect(views[0].kind === 'process' && stepThreads(views[0].steps)).toEqual(['t1'])
    }
  })
})

describe('unlinkedThreads', () => {
  it('lists only threads no item points at', () => {
    const items = [{ ...call('a', '{}'), threads: [asSessionId('t1')] }]
    expect(unlinkedThreads(items, [{ id: asSessionId('t1') }, { id: asSessionId('t2') }])).toEqual([{ id: 't2' }])
  })
})

const JOB_1 = { kind: 'job', job_id: 1 } as const
const src = (raw: string): ItemSource =>
  raw.startsWith('job:') ? { kind: 'job', job_id: Number(raw.slice(4)) } : { kind: 'thread', session_id: asSessionId(raw.slice(7)) }

describe('session events', () => {
  it('become their own block with the source, not part of a user bubble', () => {
    expect(toBlocks([event(JOB_1, 'job 1 finished'), user('hi')])).toEqual([
      { kind: 'event', source: JOB_1, text: 'job 1 finished', checkIn: false, at: '10:00' },
      { kind: 'user', text: 'hi', attachments: [] },
    ])
  })
})

describe('foldCheckIns', () => {
  const check = (source: string, text: string, at: string): Item => event(src(source), text, at, true)
  const ending = (source: string, text: string): Item => event(src(source), text)
  const fold = (items: Item[]) => foldCheckIns(groupSteps(toBlocks(items)))

  it('folds consecutive check-ins of a source with the filler replies between them into one line', () => {
    const views = fold([
      check('thread:t1', 'still working (1 min)', '10:01'),
      assistant('…'),
      check('thread:t1', 'still working (2 min)', '10:02'),
      assistant(' ... '),
      check('thread:t1', 'still working (3 min)', '10:03'),
      ending('thread:t1', 'finished your task'),
      assistant('它做完了'),
    ])
    expect(views).toEqual([
      {
        kind: 'checks',
        source: src('thread:t1'),
        checks: [
          { text: 'still working (1 min)', at: '10:01' },
          { text: 'still working (2 min)', at: '10:02' },
          { text: 'still working (3 min)', at: '10:03' },
        ],
      },
      { kind: 'event', source: src('thread:t1'), text: 'finished your task', checkIn: false, at: '10:00' },
      { kind: 'assistant', text: '它做完了' },
    ])
  })

  it('keeps interleaved sources apart and breaks the run on anything that is not a filler', () => {
    const views = fold([
      check('job:3', 'running', '1'),
      check('thread:t1', 'working', '2'),
      assistant('…'),
      check('job:3', 'running', '3'),
      assistant('日志里有报错，我去看看'),
      check('job:3', 'running', '4'),
    ])
    expect(views.map((v) => (v.kind === 'checks' ? `${sourceKey(v.source)}×${v.checks.length}` : v.kind))).toEqual([
      'job:3×2',
      'thread:t1×1',
      'assistant',
      'job:3×1',
    ])
  })

  it('folds thinking only when it leads to a filler reply', () => {
    const views = fold([
      check('job:3', 'running', '1'),
      thinking('还在跑，等着'),
      assistant('…'),
      check('job:3', 'running', '2'),
      thinking('输出卡住了'),
      assistant('我去看看日志'),
    ])
    expect(views.map((v) => (v.kind === 'checks' ? `${sourceKey(v.source)}×${v.checks.length}` : v.kind))).toEqual([
      'job:3×2',
      'process',
      'assistant',
    ])
  })

  it('leaves a filler reply that does not follow a check-in alone', () => {
    expect(fold([user('hi'), assistant('…')]).map((v) => v.kind)).toEqual(['user', 'assistant'])
  })
})

describe('approvals', () => {
  it('takes a call waiting for approval out of the process group as its own block', () => {
    const gated: Item = { ...call('g1', '{"skill":"ops"}'), tool_name: 'skill_run', wait: { kind: 'approval', state: 'pending' } }
    const views = groupSteps(toBlocks([call('a', '{}'), result('a', 'ok'), gated]))
    expect(views.map((v) => v.kind)).toEqual(['process', 'approval'])
    const last = views[1]
    if (last.kind !== 'approval') throw new Error('expected an approval block')
    expect([last.approval.callId, last.approval.status, last.approval.tool.name]).toEqual(['g1', 'pending', 'skill_run'])
  })
})
