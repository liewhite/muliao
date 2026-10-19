// 会话条目（历史与运行中的事件流共用 Item）到展示块的整理，纯函数，与渲染分开。

import type { Attachment, Item } from './api'

export interface ToolView {
  name: string
  args: string
  result: string | null
  failed: boolean
}

export type Block =
  | { kind: 'user'; text: string; attachments: Attachment[] }
  | { kind: 'assistant'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; tool: ToolView }

/** 过程步骤：思考与工具调用。连续的步骤收进一个折叠组，回答与说明文字留在组外 */
export type Step = Extract<Block, { kind: 'thinking' | 'tool' }>
export type View = Exclude<Block, Step> | { kind: 'process'; steps: Step[] }

/** 把扁平条目整理成展示块：工具调用与其结果（按 tool_call_id）合并为一块。 */
export function toBlocks(items: Item[]): Block[] {
  const blocks: Block[] = []
  const tools = new Map<string, ToolView>()
  for (const item of items) {
    switch (item.type) {
      case 'user':
        blocks.push({ kind: 'user', text: item.text, attachments: item.attachments })
        break
      case 'assistant':
      case 'thinking':
        blocks.push({ kind: item.type, text: item.text })
        break
      case 'tool_call': {
        const tool: ToolView = { name: item.tool_name ?? '?', args: item.text, result: null, failed: false }
        if (item.tool_call_id) tools.set(item.tool_call_id, tool)
        blocks.push({ kind: 'tool', tool })
        break
      }
      case 'tool_result':
      case 'tool_error': {
        const tool = item.tool_call_id ? tools.get(item.tool_call_id) : undefined
        if (tool) {
          tool.result = item.text
          tool.failed = item.type === 'tool_error'
        } else {
          // 没有对应调用的错误（如输出校验重试）单独展示
          blocks.push({ kind: 'tool', tool: { name: item.tool_name ?? 'retry', args: '', result: item.text, failed: true } })
        }
        break
      }
    }
  }
  return blocks
}

export type ToolStatus = 'running' | 'failed' | 'done'

/** 工具状态的唯一判定：历史只在一轮成功后写入，所以没有结果的调用只出现在运行中的实时流里 */
export function toolStatus(tool: ToolView): ToolStatus {
  return tool.result === null ? 'running' : tool.failed ? 'failed' : 'done'
}

const isStep = (b: Block): b is Step => b.kind === 'thinking' || b.kind === 'tool'

/** 连续的思考与工具调用合成一个过程组；其余块原样保留 */
export function groupSteps(blocks: Block[]): View[] {
  const views: View[] = []
  for (const block of blocks) {
    const last = views[views.length - 1]
    if (!isStep(block)) views.push(block)
    else if (last?.kind === 'process') last.steps.push(block)
    else views.push({ kind: 'process', steps: [block] })
  }
  return views
}

export interface StepsSummary {
  /** 如「思考 · 8 次工具调用」 */
  label: string
  /** 还在运行的工具名；没有则为 null */
  running: string | null
  failed: number
}

export function summarizeSteps(steps: Step[]): StepsSummary {
  const tools = steps.flatMap((s) => (s.kind === 'tool' ? [s.tool] : []))
  const label = [
    ...(steps.some((s) => s.kind === 'thinking') ? ['思考'] : []),
    ...(tools.length > 0 ? [`${tools.length} 次工具调用`] : []),
  ].join(' · ')
  return {
    label,
    running: tools.find((t) => toolStatus(t) === 'running')?.name ?? null,
    failed: tools.filter((t) => toolStatus(t) === 'failed').length,
  }
}
