// 会话条目（历史与运行中的事件流共用 Item）到展示块的整理，纯函数，与渲染分开。

import { ASK_USER, type AskResult, type Attachment, type Item } from './api'

export interface ToolView {
  name: string
  args: string
  result: string | null
  failed: boolean
  /** 工具结果挂给用户的文件：显示在过程组外，折叠不影响看到它们 */
  attachments: Attachment[]
}

/**
 * 一次 ask_user 提问：是要用户操作的交互，不收进过程组。问题本身取自服务端校验过的 pending_questions（待回答时），
 * 已回答的内容取自结果；参数校验失败的调用（模型写错了）按普通工具错误展示。
 */
export interface AskView {
  callId: string
  /** 交给模型的结果文本；挂起时为 null */
  result: string | null
}

export type Block =
  | { kind: 'user'; text: string; attachments: Attachment[] }
  | { kind: 'assistant'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; tool: ToolView }
  | { kind: 'ask'; ask: AskView }
  | { kind: 'stopped' }

/** 过程步骤：思考与工具调用。连续的步骤收进一个折叠组，回答与说明文字留在组外 */
export type Step = Extract<Block, { kind: 'thinking' | 'tool' }>
export type View = Exclude<Block, Step> | { kind: 'process'; steps: Step[] }

/** 把扁平条目整理成展示块：工具调用与其结果（按 tool_call_id）合并为一块。 */
export function toBlocks(items: Item[]): Block[] {
  const blocks: Block[] = []
  const tools = new Map<string, ToolView>()
  // ask_user 调用在 blocks 里的位置：参数出错时原地换成工具错误块
  const asks = new Map<string, number>()
  for (const item of items) {
    const askAt = item.tool_call_id !== null ? asks.get(item.tool_call_id) : undefined
    if (askAt !== undefined && (item.type === 'tool_result' || item.type === 'tool_error')) {
      const block = blocks[askAt]
      if (item.type === 'tool_result' && block.kind === 'ask') block.ask.result = item.text
      else blocks[askAt] = { kind: 'tool', tool: { name: ASK_USER, args: '', result: item.text, failed: true, attachments: [] } }
      continue
    }
    if (item.type === 'tool_call' && item.tool_name === ASK_USER && item.tool_call_id !== null) {
      asks.set(item.tool_call_id, blocks.length)
      blocks.push({ kind: 'ask', ask: { callId: item.tool_call_id, result: null } })
      continue
    }
    switch (item.type) {
      case 'user':
        blocks.push({ kind: 'user', text: item.text, attachments: item.attachments })
        break
      case 'stopped':
        blocks.push({ kind: 'stopped' })
        break
      case 'assistant':
      case 'thinking':
        blocks.push({ kind: item.type, text: item.text })
        break
      case 'tool_call': {
        const tool: ToolView = { name: item.tool_name ?? '?', args: item.text, result: null, failed: false, attachments: [] }
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
          tool.attachments = item.attachments
        } else {
          // 没有对应调用的错误（如输出校验重试）单独展示
          blocks.push({
            kind: 'tool',
            tool: { name: item.tool_name ?? 'retry', args: '', result: item.text, failed: true, attachments: item.attachments },
          })
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

/** 一组步骤里工具挂给用户的全部文件，按出现顺序 */
export function stepAttachments(steps: Step[]): Attachment[] {
  return steps.flatMap((s) => (s.kind === 'tool' ? s.tool.attachments : []))
}

export interface StepsSummary {
  /** 如「思考 · 8 次工具调用」 */
  label: string
  /** 还在运行的工具名；没有则为 null */
  running: string | null
  failed: number
}

/** 提问的结果：工具结果的文本就是服务端 AskResult 的 JSON */
export function askResult(result: string): AskResult {
  return JSON.parse(result) as AskResult
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
