// 工具调用详情的展示块（后端 platform/tool_views.py 按各工具的展示规则给出）整理成要画的段，纯函数，与渲染分开。

import type { ViewBlock } from './api'

export type TerminalBlock = Extract<ViewBlock, { kind: 'terminal' }>

/** 要画的一段：相邻的终端块（调用的命令与结果的输出）合进同一个终端框，其余每块一段 */
export type Segment =
  { kind: 'terminal'; blocks: TerminalBlock[] } | { kind: 'block'; block: Exclude<ViewBlock, TerminalBlock> }

export function toSegments(blocks: ViewBlock[]): Segment[] {
  const segments: Segment[] = []
  for (const block of blocks) {
    const last = segments.at(-1)
    if (block.kind !== 'terminal') segments.push({ kind: 'block', block })
    else if (last?.kind === 'terminal') last.blocks.push(block)
    else segments.push({ kind: 'terminal', blocks: [block] })
  }
  return segments
}

/** 统一 diff 的一行属于哪一类，决定颜色 */
export function diffLineKind(line: string): 'add' | 'del' | 'hunk' | 'context' {
  if (line.startsWith('@@')) return 'hunk'
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'del'
  return 'context'
}
