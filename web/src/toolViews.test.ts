import { describe, expect, it } from 'vitest'
import type { ViewBlock } from './api'
import { diffLineKind, toSegments } from './toolViews'

const cmd: ViewBlock = { kind: 'terminal', command: 'ls', output: null, status: null, failed: false }
const out: ViewBlock = { kind: 'terminal', command: null, output: 'a', status: 'exit 0', failed: false }
const md: ViewBlock = { kind: 'markdown', title: null, text: 'x' }

describe('toSegments', () => {
  it('draws adjacent terminal blocks (command, then output) in one terminal', () => {
    expect(toSegments([cmd, out])).toEqual([{ kind: 'terminal', blocks: [cmd, out] }])
  })

  it('keeps other blocks apart and breaks terminals around them', () => {
    expect(toSegments([cmd, md, out])).toEqual([
      { kind: 'terminal', blocks: [cmd] },
      { kind: 'block', block: md },
      { kind: 'terminal', blocks: [out] },
    ])
  })
})

describe('diffLineKind', () => {
  it('classifies unified diff lines', () => {
    expect(['@@ -1 +1 @@', '+a', '-b', ' c'].map(diffLineKind)).toEqual(['hunk', 'add', 'del', 'context'])
  })
})
