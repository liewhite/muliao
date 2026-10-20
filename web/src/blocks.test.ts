import { describe, expect, it } from 'vitest'
import type { AskView, Item } from './api'
import { groupSteps, stepAttachments, stepThreads, summarizeSteps, toBlocks, unlinkedThreads, type Step } from './blocks'

const item = (type: Item['type'], text: string, tool_call_id: string | null = null): Item => ({
  type,
  text,
  tool_name: tool_call_id ? 'run_shell' : null,
  tool_call_id,
  message_id: null,
  attachments: [],
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

describe('summarizeSteps', () => {
  const tool = (result: string | null, rejected = false): Step => ({
    kind: 'tool',
    tool: { name: 'run_shell', args: '', result, rejected, attachments: [], threads: [] },
  })

  it('counts tool calls, mentions thinking, and reports the running tool and calls returned to the model', () => {
    expect(summarizeSteps([{ kind: 'thinking', text: 'x' }, tool('ok'), tool('err', true), tool(null)])).toEqual({
      label: '思考 · 3 次工具调用',
      running: 'run_shell',
      rejected: 1,
    })
    expect(summarizeSteps([tool('ok')])).toEqual({ label: '1 次工具调用', running: null, rejected: 0 })
    expect(summarizeSteps([{ kind: 'thinking', text: 'x' }]).label).toBe('思考')
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

  it('shows a rejected call as a rejected tool step', () => {
    // 参数不合法的提问后端不给状态，按普通工具调用展示；参数合法却被退回（如这次运行不提供提问）原地换成工具错误
    for (const ask of [null, state]) {
      const rejected = toBlocks([call('{"questions": []}', ask), result('tool_error', 'too short')])
      expect(rejected.map((b) => (b.kind === 'tool' ? [b.kind, b.tool.rejected] : [b.kind]))).toEqual([['tool', true]])
    }
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
      { kind: 'event', source: 'job:1', text: 'job 1 finished' },
      { kind: 'user', text: 'hi', attachments: [] },
    ])
  })
})
