// 会话条目（历史与运行中的事件流共用 Item）到展示块的整理，纯函数，与渲染分开。

import { type AskView, type Attachment, type EventSource, type Item, type ViewBlock } from './api'

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
  /** 参数、结果按工具的展示规则画成的块（调用、结果条目上的 view，由后端给出）；null 时显示原文 */
  callView: ViewBlock[] | null
  resultView: ViewBlock[] | null
}

/** 一次 ask_user 提问：是要用户操作的交互，不收进过程组。状态与内容都由后端给出（条目上的 ask） */
export interface AskBlock {
  callId: string
  ask: AskView
}

export type Block =
  | { kind: 'user'; text: string; attachments: Attachment[] }
  /** checkIn：运行中检查（工作还在进行，后端显式标记）；at：交给 agent 的时间 */
  | { kind: 'event'; source: EventSource; text: string; checkIn: boolean; at: string | null }
  | { kind: 'assistant'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; tool: ToolView }
  | { kind: 'ask'; ask: AskBlock }
  | { kind: 'stopped' }

/** 过程步骤：思考与工具调用。连续的步骤收进一个折叠组，回答与说明文字留在组外 */
export type Step = Extract<Block, { kind: 'thinking' | 'tool' }>
/** 同一来源的一串运行中检查（foldCheckIns） */
export interface CheckIn {
  text: string
  at: string | null
}
export type View =
  | Exclude<Block, Step>
  | { kind: 'process'; steps: Step[] }
  | { kind: 'checks'; source: EventSource; checks: CheckIn[] }

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
        if (item.source === null) throw new Error('event item without a source')
        blocks.push({ kind: 'event', source: item.source, text: item.text, checkIn: item.check_in, at: item.at })
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
          threads: item.threads,
          startedAt: item.at,
          finishedAt: null,
          callView: item.view,
          resultView: null,
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
          tool.finishedAt = item.at
          tool.resultView = item.view
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
              callView: null,
              resultView: item.view,
            },
          })
        }
        break
      }
    }
  }
  return blocks
}

export type ToolStatus = 'running' | 'unfinished' | 'rejected' | 'done'

/**
 * 工具状态的唯一判定。live：条目是否来自正在进行的运行。没有结果的调用只有在当前运行里才在执行；
 * 历史里的（运行在执行它时失败、等重试或续跑）并没有在执行，是未完成
 */
export function toolStatus(tool: ToolView, live: boolean): ToolStatus {
  if (tool.result === null) return live ? 'running' : 'unfinished'
  return tool.rejected ? 'rejected' : 'done'
}

const isStep = (b: Block): b is Step => b.kind === 'thinking' || b.kind === 'tool'

/** 这一步有没有不随过程折叠的成果：与组下方显示的同一来源（stepAttachments、stepThreads） */
const hasOutputs = (s: Step) => stepAttachments([s]).length > 0 || stepThreads([s]).length > 0

/**
 * 连续的思考与工具调用合成一个过程组；其余块原样保留。成果（文件、thread）显示在组下方，所以有成果的组到此为止：
 * 之后不带成果的步骤另起一组，否则成果之后才做的调用会折进成果上面的组里，看起来像发生在它之前。紧挨着的带成果
 * 步骤（如一次并行开出几个 thread）仍在一组。文件随结果到达，切出的新组总在末尾；实时调用条目不带 thread，保存一步
 * 重新拉取后才挂上，那时实时段里 thread 之后的步骤会从组中间切开（已知，下一步保存即与历史一致）
 */
export function groupSteps(blocks: Block[]): View[] {
  const views: View[] = []
  let open: { steps: Step[]; hasOutputs: boolean } | null = null
  for (const block of blocks) {
    if (!isStep(block)) {
      views.push(block)
      open = null
      continue
    }
    const outputs = hasOutputs(block)
    if (open === null || (open.hasOutputs && !outputs)) {
      open = { steps: [], hasOutputs: false }
      views.push({ kind: 'process', steps: open.steps })
    }
    open.steps.push(block)
    open.hasOutputs ||= outputs
  }
  return views
}

/** agent 对检查只回的占位答复（全局约定：没有新进展时回“…”），合并检查时一起收起 */
const isFiller = (v: View | undefined) => v?.kind === 'assistant' && ['', '…', '...'].includes(v.text.trim())
const onlyThinking = (v: View) => v.kind === 'process' && v.steps.every((s) => s.kind === 'thinking')

/**
 * 连续的运行中检查（夹在中间的占位答复“…”、以及紧接着占位答复的纯思考一并收起）按来源各合成一条，按该来源第一次
 * 出现的位置排。别的任何块（用户消息、结局事件、工具调用、实际回答）都会断开这一串；结局事件照常单独显示
 */
export function foldCheckIns(views: View[]): View[] {
  const folded: View[] = []
  let run: Map<string, CheckIn[]> | null = null
  for (const [i, view] of views.entries()) {
    if (run !== null && onlyThinking(view) && isFiller(views[i + 1])) continue
    if (view.kind === 'event' && view.checkIn) {
      run ??= new Map()
      const checks = run.get(view.source.raw)
      const check = { text: view.text, at: view.at }
      if (checks) checks.push(check)
      else {
        const fresh = [check]
        run.set(view.source.raw, fresh)
        folded.push({ kind: 'checks', source: view.source, checks: fresh })
      }
    } else if (run !== null && isFiller(view)) {
      continue
    } else {
      run = null
      folded.push(view)
    }
  }
  return folded
}

/** 一组步骤里委派开出或续写的 thread（子会话 id），按出现顺序、去重 */
export function stepThreads(steps: Step[]): string[] {
  return [...new Set(steps.flatMap((s) => (s.kind === 'tool' ? s.tool.threads : [])))]
}

/**
 * 没有被任何调用条目引用的 thread（后端按调用 id 给调用条目挂 thread，结果条目不带）。引用来自完整历史（被压缩的
 * 部分也算）与正在进行的这一段；经 SSE 刚收到、还没重新拉取的调用暂时不带。
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
  /** 历史里没有结果、也不在执行的调用数 */
  unfinished: number
  /** 被退回、由模型修正的调用数 */
  rejected: number
}

export function summarizeSteps(steps: Step[], live: boolean): StepsSummary {
  const tools = steps.flatMap((s) => (s.kind === 'tool' ? [s.tool] : []))
  const label = [
    ...(steps.some((s) => s.kind === 'thinking') ? ['思考'] : []),
    ...(tools.length > 0 ? [`${tools.length} 次工具调用`] : []),
  ].join(' · ')
  const statuses = tools.map((t) => toolStatus(t, live))
  return {
    label,
    running: tools.find((_, i) => statuses[i] === 'running')?.name ?? null,
    unfinished: statuses.filter((s) => s === 'unfinished').length,
    rejected: statuses.filter((s) => s === 'rejected').length,
  }
}
