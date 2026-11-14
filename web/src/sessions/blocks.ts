// 会话条目（历史与运行中的事件流共用 Item）到展示块的整理，纯函数，与渲染分开。

import {
  type AskView,
  type Attachment,
  type CallId,
  type ItemSource,
  type Item,
  type SessionId,
  type ViewBlock,
} from '../api'
import { assertNever } from '../exhaustive'

/** 同一来源的事件归在一起用的键 */
export function sourceKey(s: ItemSource): string {
  switch (s.kind) {
    case 'job':
      return `job:${s.job_id}`
    case 'thread':
      return `thread:${s.session_id}`
    case 'wake':
      return 'wake'
  }
}

/** 调用参数原文里值得显示的：没有参数（空串，或解析出空对象，不管模型怎么写空白）时为 null；不是合法 JSON 的照原文显示 */
export function argsShown(text: string): string | null {
  if (text.trim() === '') return null
  try {
    const v: unknown = JSON.parse(text)
    if (typeof v === 'object' && v !== null && !Array.isArray(v) && Object.keys(v).length === 0) return null
  } catch {
    // 不是 JSON：照原文显示
  }
  return text
}

/** 一次工具调用的调用这一半（调用条目给出） */
export interface ToolCall {
  /** 参数原文；没有参数（空串、空对象）时为 null，不显示 */
  args: string | null
  /** 开始执行的时间（由后端给出）；拿不到的为 null */
  at: string | null
  /** 参数按工具的展示规则画成的块；null 时显示原文 */
  view: ViewBlock[] | null
}

/** 一次工具调用的结果这一半（结果或错误条目给出） */
export interface ToolOutcome {
  text: string
  /** 被退回：参数校验失败或工具报了可纠正的错误，已交还模型修正（真正的执行失败会让整轮失败，不在这里） */
  rejected: boolean
  /** 工具结果挂给用户的文件：显示在过程组外，折叠不影响看到它们 */
  attachments: Attachment[]
  /** 工具交回给模型的图片（如浏览器截图）：属于这次调用的过程，显示在调用里，随过程组折叠 */
  images: Attachment[]
  /** 结束的时间（由后端给出）；拿不到的为 null */
  at: string | null
  /** 结果按工具的展示规则画成的块；null 时显示原文 */
  view: ViewBlock[] | null
}

/** 有调用的工具：还没有结果（在执行或没做完）时 outcome 为 null */
export interface CalledTool {
  name: string
  /** 调用关联的子会话 id（委派开出的 thread）：显示在过程组外，就在开它的那次调用处 */
  threads: SessionId[]
  call: ToolCall
  outcome: ToolOutcome | null
}

/** 工具调用的展示：有调用的；或只有结果、没有对应调用的（调用在压缩前或运行中还没保存的那一段里） */
export type ToolView = CalledTool | { name: string; threads: SessionId[]; call: null; outcome: ToolOutcome }

/** 一次 ask_user 提问：是要用户操作的交互，不收进过程组。状态与内容都由后端给出（条目上的 ask） */
export interface AskBlock {
  callId: CallId
  ask: AskView
}

/** 停下来等用户批准的工具调用（条目上的 approval，由后端给出）：要用户操作，不收进过程组 */
export interface ApprovalBlock {
  callId: CallId
  status: 'pending' | 'submitted'
  tool: CalledTool
}

export type Block =
  | { kind: 'user'; text: string; attachments: Attachment[] }
  /** checkIn：运行中检查（工作还在进行，后端显式标记）；at：交给 agent 的时间 */
  | { kind: 'event'; source: ItemSource; text: string; checkIn: boolean; at: string | null }
  /** noProgress：只是运行中检查时没有新进展的占位答复（服务端判断），合并检查时一起收起 */
  | { kind: 'assistant'; text: string; noProgress: boolean }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; tool: ToolView }
  | { kind: 'ask'; ask: AskBlock }
  | { kind: 'approval'; approval: ApprovalBlock }
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
  | { kind: 'checks'; source: ItemSource; checks: CheckIn[] }

/** 把扁平条目整理成展示块：工具调用与其结果（按 tool_call_id）合并为一块。 */
export function toBlocks(items: Item[]): Block[] {
  const blocks: Block[] = []
  const tools = new Map<string, CalledTool>()
  // 提问的调用：结局已经体现在调用的状态里（后端 sessions 的 annotate_asks），结果不再单独成块。被退回的提问后端不给
  // 状态，按普通工具调用展示；运行中还没保存的那一段里它显示为「提问中」，保存后以历史为准
  const asks = new Set<string>()
  for (const item of items) {
    switch (item.type) {
      case 'user':
        blocks.push({ kind: 'user', text: item.text, attachments: item.attachments })
        break
      case 'event':
        blocks.push({ kind: 'event', source: item.source, text: item.text, checkIn: item.check_in, at: item.at })
        break
      case 'stopped':
        blocks.push({ kind: 'stopped' })
        break
      case 'assistant':
        blocks.push({ kind: 'assistant', text: item.text, noProgress: item.no_progress })
        break
      case 'thinking':
        blocks.push({ kind: 'thinking', text: item.text })
        break
      case 'tool_call': {
        if (item.wait?.kind === 'ask') {
          asks.add(item.tool_call_id)
          blocks.push({ kind: 'ask', ask: { callId: item.tool_call_id, ask: item.wait } })
          break
        }
        const tool: CalledTool = {
          name: item.tool_name,
          threads: item.threads,
          call: { args: argsShown(item.text), at: item.at, view: item.view },
          outcome: null,
        }
        tools.set(item.tool_call_id, tool)
        if (item.wait?.kind === 'approval') {
          blocks.push({ kind: 'approval', approval: { callId: item.tool_call_id, status: item.wait.state, tool } })
        } else {
          blocks.push({ kind: 'tool', tool })
        }
        break
      }
      case 'tool_result':
      case 'tool_error': {
        if (asks.has(item.tool_call_id)) break
        const output = item.type === 'tool_result' ? item : { attachments: [], images: [], view: null }
        const outcome: ToolOutcome = {
          text: item.text,
          rejected: item.type === 'tool_error',
          attachments: output.attachments,
          images: output.images,
          at: item.at,
          view: output.view,
        }
        const tool = tools.get(item.tool_call_id)
        if (tool) {
          tool.outcome = outcome
        } else {
          // 没有对应调用的结果（调用在压缩前或运行中还没保存的那一段里）单独展示
          blocks.push({ kind: 'tool', tool: { name: item.tool_name, threads: [], call: null, outcome } })
        }
        break
      }
      default:
        assertNever(item)
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
  if (tool.outcome === null) return live ? 'running' : 'unfinished'
  return tool.outcome.rejected ? 'rejected' : 'done'
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

/** agent 对检查只回的占位答复，合并检查时一起收起 */
const isFiller = (v: View | undefined) => v?.kind === 'assistant' && v.noProgress
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
      const checks = run.get(sourceKey(view.source))
      const check = { text: view.text, at: view.at }
      if (checks) checks.push(check)
      else {
        const fresh = [check]
        run.set(sourceKey(view.source), fresh)
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
export function stepThreads(steps: Step[]): SessionId[] {
  return [...new Set(steps.flatMap((s) => (s.kind === 'tool' ? s.tool.threads : [])))]
}

/**
 * 没有被任何调用条目引用的 thread（后端按调用 id 给调用条目挂 thread，结果条目不带）。引用来自完整历史（被压缩的
 * 部分也算）与正在进行的这一段；经 SSE 刚收到、还没重新拉取的调用暂时不带。
 */
export function unlinkedThreads<T extends { id: SessionId }>(items: Item[], threads: T[]): T[] {
  const linked = new Set(items.flatMap((i) => (i.type === 'tool_call' ? i.threads : [])))
  return threads.filter((t) => !linked.has(t.id))
}

/** 一组步骤里工具挂给用户的全部文件，按出现顺序 */
export function stepAttachments(steps: Step[]): Attachment[] {
  return steps.flatMap((s) => (s.kind === 'tool' ? (s.tool.outcome?.attachments ?? []) : []))
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
