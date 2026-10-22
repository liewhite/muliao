// 会话条目（历史与运行中的事件流共用 Item）到展示块的整理，纯函数，与渲染分开。

import { type AskView, type Attachment, type Item } from './api'

export interface ToolView {
  name: string
  args: string
  result: string | null
  /** 被退回：参数校验失败或工具报了可纠正的错误，已交还模型修正（真正的执行失败会让整轮失败，不在这里） */
  rejected: boolean
  /** 工具结果挂给用户的文件：显示在过程组外，折叠不影响看到它们 */
  attachments: Attachment[]
  /** 工具交回给模型的图片（如浏览器截图）：属于这次调用的过程，显示在调用里，随过程组折叠 */
  images: Attachment[]
  /** 工具结果关联的子会话 id（委派开出的 thread）：同样显示在过程组外，就在开它的那次调用处 */
  threads: string[]
  /** 开始执行、结束的时间（调用与结果条目上的 at，由后端给出）；拿不到的为 null */
  startedAt: string | null
  finishedAt: string | null
}

/** 一次 ask_user 提问：是要用户操作的交互，不收进过程组。状态与内容都由后端给出（条目上的 ask） */
export interface AskBlock {
  callId: string
  ask: AskView
}

export type Block =
  | { kind: 'user'; text: string; attachments: Attachment[] }
  | { kind: 'event'; source: string; text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; tool: ToolView }
  | { kind: 'ask'; ask: AskBlock }
  | { kind: 'stopped' }

/** 过程步骤：思考与工具调用。连续的步骤收进一个折叠组，回答与说明文字留在组外 */
export type Step = Extract<Block, { kind: 'thinking' | 'tool' }>
export type View = Exclude<Block, Step> | { kind: 'process'; steps: Step[] }

/** 把扁平条目整理成展示块：工具调用与其结果（按 tool_call_id）合并为一块。 */
export function toBlocks(items: Item[]): Block[] {
  const blocks: Block[] = []
  const tools = new Map<string, ToolView>()
  // 提问的调用：结局已经体现在调用的状态里（后端 transcript.annotate_asks），结果不再单独成块。被退回的提问后端不给
  // 状态，按普通工具调用展示；运行中还没保存的那一段里它显示为「提问中」，保存后以历史为准
  const asks = new Set<string>()
  for (const item of items) {
    if (item.tool_call_id !== null && asks.has(item.tool_call_id)) continue
    if (item.ask !== null && item.tool_call_id !== null) {
      asks.add(item.tool_call_id)
      blocks.push({ kind: 'ask', ask: { callId: item.tool_call_id, ask: item.ask } })
      continue
    }
    switch (item.type) {
      case 'user':
        blocks.push({ kind: 'user', text: item.text, attachments: item.attachments })
        break
      case 'event':
        if (item.tool_name === null) throw new Error('event item without a source')
        blocks.push({ kind: 'event', source: item.tool_name, text: item.text })
        break
      case 'stopped':
        blocks.push({ kind: 'stopped' })
        break
      case 'assistant':
      case 'thinking':
        blocks.push({ kind: item.type, text: item.text })
        break
      case 'tool_call': {
        const tool: ToolView = {
          name: item.tool_name ?? '?',
          args: item.text,
          result: null,
          rejected: false,
          attachments: [],
          images: [],
          threads: [],
          startedAt: item.at,
          finishedAt: null,
        }
        if (item.tool_call_id) tools.set(item.tool_call_id, tool)
        blocks.push({ kind: 'tool', tool })
        break
      }
      case 'tool_result':
      case 'tool_error': {
        const tool = item.tool_call_id ? tools.get(item.tool_call_id) : undefined
        if (tool) {
          tool.result = item.text
          tool.rejected = item.type === 'tool_error'
          tool.attachments = item.attachments
          tool.images = item.images
          tool.threads = item.threads
          tool.finishedAt = item.at
        } else {
          // 没有对应调用的错误（如输出校验重试）单独展示
          blocks.push({
            kind: 'tool',
            tool: {
              name: item.tool_name ?? 'retry',
              args: '',
              result: item.text,
              rejected: true,
              attachments: item.attachments,
              images: item.images,
              threads: item.threads,
              startedAt: null,
              finishedAt: item.at,
            },
          })
        }
        break
      }
    }
  }
  return blocks
}

export type ToolStatus = 'running' | 'rejected' | 'done'

/** 工具状态的唯一判定：没有结果的调用在执行中（运行中的这一段），或运行在执行它时失败、等重试再执行 */
export function toolStatus(tool: ToolView): ToolStatus {
  return tool.result === null ? 'running' : tool.rejected ? 'rejected' : 'done'
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

/** 一组步骤里委派开出或续写的 thread（子会话 id），按出现顺序、去重 */
export function stepThreads(steps: Step[]): string[] {
  return [...new Set(steps.flatMap((s) => (s.kind === 'tool' ? s.tool.threads : [])))]
}

/**
 * 没有被任何条目引用的 thread：在 thread 随调用记录之前开出的、开出后调用被停止或失败的。
 * 引用来自完整历史（被压缩的部分也算）。
 */
export function unlinkedThreads<T extends { id: string }>(items: Item[], threads: T[]): T[] {
  const linked = new Set(items.flatMap((i) => i.threads))
  return threads.filter((t) => !linked.has(t.id))
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
  /** 被退回、由模型修正的调用数 */
  rejected: number
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
    rejected: tools.filter((t) => toolStatus(t) === 'rejected').length,
  }
}
