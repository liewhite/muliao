import { describe, expect, it } from 'vitest'
import type { Step, ToolView } from './blocks'
import { formatClock, formatDuration, groupSpan, spanLabel, toolSpan } from './stepTime'

const tool = (startedAt: string | null, finishedAt: string | null, result: string | null = 'ok'): ToolView => ({
  name: 'run_shell',
  args: '{}',
  result,
  rejected: false,
  attachments: [],
  threads: [],
  startedAt,
  finishedAt,
})
const step = (t: ToolView): Step => ({ kind: 'tool', tool: t })
const T0 = '2026-10-22T05:00:00Z'
const ms = (iso: string) => Date.parse(iso)

describe('formatDuration', () => {
  it('uses tenths under ten seconds, whole seconds, then minutes and hours', () => {
    expect(formatDuration(4230)).toBe('4.2s')
    expect(formatDuration(42_900)).toBe('42s')
    expect(formatDuration(83_000)).toBe('1m23s')
    expect(formatDuration(3_725_000)).toBe('1h02m')
  })

  it('never shows a negative duration', () => {
    expect(formatDuration(-500)).toBe('0.0s')
  })
})

describe('formatClock', () => {
  it('shows only the time today and adds the date otherwise', () => {
    const at = new Date(2026, 9, 22, 13, 5, 12).getTime()
    expect(formatClock(at, new Date(2026, 9, 22, 18).getTime())).toBe('13:05:12')
    expect(formatClock(at, new Date(2026, 9, 23, 1).getTime())).toBe('10-22 13:05:12')
  })
})

describe('toolSpan', () => {
  it('runs from the call start to the result', () => {
    expect(toolSpan(tool(T0, '2026-10-22T05:00:04.2Z'))).toEqual({ start: ms(T0), end: ms(T0) + 4200, running: false })
  })

  it('is running while the call has no result', () => {
    expect(toolSpan(tool(T0, null, null))).toEqual({ start: ms(T0), end: null, running: true })
  })

  it('has no end when the end is unknown and nothing without a start', () => {
    expect(toolSpan(tool(T0, null))).toEqual({ start: ms(T0), end: null, running: false })
    expect(toolSpan(tool(null, '2026-10-22T05:00:04Z'))).toBeNull()
  })
})

describe('groupSpan', () => {
  it('spans from the earliest start to the latest end', () => {
    const span = groupSpan([
      step(tool('2026-10-22T05:00:10Z', '2026-10-22T05:00:20Z')),
      { kind: 'thinking', text: '...' },
      step(tool(T0, '2026-10-22T05:00:05Z')),
    ])
    expect(span).toEqual({ start: ms(T0), end: ms('2026-10-22T05:00:20Z'), running: false })
  })

  it('is running while any tool runs, and has no end when one end is unknown', () => {
    expect(groupSpan([step(tool(T0, '2026-10-22T05:00:05Z')), step(tool(T0, null, null))])?.running).toBe(true)
    expect(groupSpan([step(tool(T0, '2026-10-22T05:00:05Z')), step(tool(T0, null))])?.end).toBeNull()
  })

  it('is absent without any timed tool', () => {
    expect(groupSpan([{ kind: 'thinking', text: '...' }, step(tool(null, null))])).toBeNull()
  })
})

describe('spanLabel', () => {
  const now = ms(T0) + 83_000
  it('shows elapsed time while running', () => {
    expect(spanLabel({ start: ms(T0), end: null, running: true }, now)).toBe('已运行 1m23s')
  })

  it('shows start time and duration when finished, start only when the end is unknown', () => {
    const start = formatClock(ms(T0), now)
    expect(spanLabel({ start: ms(T0), end: ms(T0) + 4200, running: false }, now)).toBe(`${start} · 4.2s`)
    expect(spanLabel({ start: ms(T0), end: null, running: false }, now)).toBe(start)
  })
})
