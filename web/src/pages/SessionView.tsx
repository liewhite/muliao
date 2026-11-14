import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Button, Chip, Disclosure, Spinner, TextArea, TextField, toast } from '@heroui/react'
import {
  AlarmClock,
  Archive,
  ArrowDown,
  BellDot,
  CircleStop,
  CornerLeftUp,
  GitBranch,
  Hourglass,
  MessageCircleQuestion,
  RotateCcw,
  ShieldQuestion,
  SendHorizontal,
  Square,
  SquareTerminal,
  Trash2,
} from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import {
  agentRoute,
  api,
  asSessionId,
  type AgentId,
  type SessionId,
  type ApprovalChoice,
  type Attachment,
  type CompactionInfo,
  type ItemSource,
  type Item,
  type SavedItem,
  type QuestionAnswer,
  type Mail,
  type RunRecord,
  type Retrying,
  type Thread,
  type SessionDetail,
  type Wait,
  type IssueId,
  type CallId,
  sessionRoute,
} from '../api'
import { approvalAnchor, askAnchor, waitDetail, waitKey } from '../waits'
import {
  foldCheckIns,
  groupSteps,
  stepAttachments,
  stepThreads,
  unlinkedThreads,
  summarizeSteps,
  toBlocks,
  toolStatus,
  type ApprovalBlock as ApprovalBlockData,
  type AskBlock as AskBlockData,
  type CheckIn,
  type Step,
  type ToolStatus,
  type ToolView,
} from '../blocks'
import { ToolViewBlocks } from '../ToolViewBlocks'
import { groupSpan, isRunning, spanLabel, toolSpan, useNow, type Span } from '../stepTime'
import { AnsweredQuestions, QuestionForm, QuestionNote } from '../questions'
import { AttachButton, AttachmentList, PendingUploads, useUploads, type Uploads } from '../attachments'
import { appendTranscript, useVoiceInput, type VoiceInput } from '../voice'
import { VoiceButton } from '../VoiceButton'
import { isSendShortcut, SEND_HINT } from '../sendKey'
import { liveItems, mergeLive, useServerEvents, withoutCovered, type PageEvent, type RunEvent } from '../events'
import { STATUS_LABEL, issueRoute } from '../issues'
import { useReturnState, useRouteParam } from '../nav'
import { cacheHitPercent, contextText, formatTokens } from '../usage'
import { StatusDot } from './Issues'
import { ActionButton, AgentName, CODE_BLOCK, ErrorAlert, Loading, Markdown, Page, formatTime, useAction, useConfirm } from '../ui'
import { useLoad } from '../load'

/**
 * issue 会话的页头标题：指回它的 issue，显示编号、标题、当前状态（随 issue 变化刷新）；在 issue 页返回回到这个会话。
 * 取不到 issue（如已删除，会话保留）时显示会话自己的标题和原因
 */
function IssueTitle({ issueId, fallback }: { issueId: IssueId; fallback: string }) {
  const from = useReturnState()
  const detail = useLoad(() => api.issue(issueId), [issueId])
  const issue = detail.data?.issue
  return (
    <Link
      to={issueRoute(issueId)}
      state={from}
      className="flex min-w-0 items-center gap-1.5 hover:text-accent"
    >
      {issue && <StatusDot status={issue.status} />}
      <span className="min-w-0 truncate">{issue ? `#${issueId} ${issue.title}` : fallback}</span>
      {issue && <span className="shrink-0 text-xs font-normal text-muted">{STATUS_LABEL[issue.status]}</span>}
      {detail.error && <span className="shrink-0 text-xs font-normal text-danger">{detail.error}</span>}
    </Link>
  )
}

/** lead：放在展开按钮之前、自己可以点的内容（如指向 thread 的链接），不能嵌在按钮里 */
function Collapsible({ title, children, lead }: { title: ReactNode; children: ReactNode; lead?: ReactNode }) {
  return (
    <Disclosure
      className="max-w-[88%] self-start rounded-xl border border-dashed border-separator px-3 py-1.5 text-sm text-muted"
    >
      <Disclosure.Heading className="flex items-center gap-2">
        {lead}
        <Disclosure.Trigger className="flex min-w-0 flex-1 items-center gap-2 text-left">
          {title}
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-1">{children}</Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

// 被退回不是故障：参数不合法或工具报了可纠正的错误，模型会据此修正后重试，所以不用醒目的报错样式
const TOOL_STATUS_LABELS: Record<ToolStatus, string> = { running: '运行中', unfinished: '未完成', rejected: '被退回', done: '完成' }

/** 步骤的时间：运行中每秒刷新已运行时长，结束后是开始时间与耗时；没有时间时不显示 */
function SpanText({ span }: { span: Span | null }) {
  const now = useNow(isRunning(span))
  return span && <span className="tabular-nums">{spanLabel(span, now)}</span>
}

/** 参数与结果：有展示块的一边按块渲染，否则（或切到原文时）显示原文 */
function ToolDetail({ tool }: { tool: ToolView }) {
  const [raw, setRaw] = useState(false)
  const views = [tool.call?.view ?? null, tool.outcome?.view ?? null]
  const rendered = views.some((v) => v !== null)
  const [callView, resultView] = raw ? [null, null] : views
  return (
    <>
      {callView !== null && resultView !== null ? (
        // 两边都按块画时合在一起：调用的命令与结果的输出画进同一个终端框
        <ToolViewBlocks blocks={[...callView, ...resultView]} />
      ) : (
        <>
          {callView !== null ? (
            <ToolViewBlocks blocks={callView} />
          ) : (
            tool.call && tool.call.args !== '{}' && <pre className={CODE_BLOCK}>{tool.call.args}</pre>
          )}
          {resultView !== null ? (
            <ToolViewBlocks blocks={resultView} />
          ) : (
            tool.outcome && <pre className={CODE_BLOCK}>{tool.outcome.text}</pre>
          )}
        </>
      )}
      {rendered && (
        <div className="flex justify-end">
          <button type="button" className="text-xs text-muted hover:text-foreground" onClick={() => setRaw(!raw)}>
            {raw ? '渲染' : '原文'}
          </button>
        </div>
      )}
    </>
  )
}

function ToolBlock({ tool, live }: { tool: ToolView; live: boolean }) {
  const status = toolStatus(tool, live)
  return (
    <Collapsible
      title={
        <>
          <span className="font-mono text-foreground">{tool.name}</span>
          <span className="ml-auto flex gap-2 whitespace-nowrap text-xs">
            <SpanText span={toolSpan(tool, live)} />
            <span>{TOOL_STATUS_LABELS[status]}</span>
          </span>
        </>
      }
    >
      <ToolDetail tool={tool} />
      {tool.outcome && <AttachmentList files={tool.outcome.images} />}
    </Collapsible>
  )
}

function UserBubble({ text, attachments, pending }: { text: string; attachments: Attachment[]; pending?: boolean }) {
  return (
    <div className={`flex flex-col items-end gap-1.5 self-stretch ${pending ? 'opacity-70' : ''}`}>
      <AttachmentList files={attachments} align="end" />
      {text && (
        <div className="max-w-[88%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent px-3.5 py-2.5 text-accent-foreground">
          {text}
        </div>
      )}
    </div>
  )
}

function StepBlock({ step, live }: { step: Step; live: boolean }) {
  return step.kind === 'tool' ? (
    <ToolBlock tool={step.tool} live={live} />
  ) : (
    <Collapsible title="思考">
      <div className="whitespace-pre-wrap">{step.text}</div>
    </Collapsible>
  )
}

/** 连续的思考与工具调用收成一组，默认折叠；标题汇总步骤数与状态，展开后每一步仍可单独展开。工具发出的文件显示在组下方 */
function StepsGroup({ steps, ctx }: { steps: Step[]; ctx: SessionContext }) {
  const live = ctx.view === 'live'
  const { label, running, unfinished, rejected } = summarizeSteps(steps, live)
  const status =
    running !== null
      ? `运行中：${running}`
      : [unfinished > 0 ? `${unfinished} 次未完成` : '完成', ...(rejected > 0 ? [`${rejected} 次被退回`] : [])].join(' · ')
  return (
    <>
      <Collapsible
        title={
          <>
            {running !== null && <Spinner size="sm" />}
            <span className="text-foreground">{label}</span>
            <span className="ml-auto flex gap-2 whitespace-nowrap text-xs">
              <SpanText span={groupSpan(steps, live)} />
              <span>{status}</span>
            </span>
          </>
        }
      >
        <div className="flex flex-col gap-2">
          {steps.map((step, i) => (
            <StepBlock key={i} step={step} live={live} />
          ))}
        </div>
      </Collapsible>
      {/* 工具发给用户的文件、委派开出的 thread 是成果，不随过程一起折叠，就显示在开它们的那次调用处 */}
      <AttachmentList files={stepAttachments(steps)} />
      <ThreadLinks ids={stepThreads(steps)} ctx={ctx} />
    </>
  )
}

/** 渲染会话条目要用到的会话信息：回答提问的方式、这个会话开出的 thread、agent 名称 */
interface SessionContext {
  answer: (callId: CallId, answers: QuestionAnswer[]) => Promise<unknown>
  /** 批准、拒绝或始终允许一次等批准的工具调用 */
  approve: (callId: CallId, choice: ApprovalChoice) => Promise<unknown>
  threads: Map<SessionId, Thread>
  /**
   * 条目来自哪里：
   * - history：数据库里的历史，与会话的 thread 列表来自同一个响应，引用的 thread 缺失就是已被删除；
   * - live：运行中的条目，thread 可能刚开出、还没重新拉取
   */
  view: 'history' | 'live'
}

/** 用户对等批准的调用的选择，显示在已提交的决定处 */
const CHOICE_TEXT: Record<ApprovalChoice, string> = { approve: '已批准', always: '已始终允许', deny: '已拒绝' }

function AskBlock({ block, ctx }: { block: AskBlockData; ctx: SessionContext }) {
  const { ask } = block
  const { state } = ask
  switch (state.status) {
    case 'pending':
      return (
        // 等待项里的「跳到提问」滚到这里
        <div id={askAnchor(block.callId)} className="flex scroll-mt-4 flex-col">
          <QuestionForm questions={ask.questions} onSubmit={(answers) => ctx.answer(block.callId, answers)} />
        </div>
      )
    case 'asking':
      return <QuestionNote text="提问中…" />
    // 回答显示在信箱里
    case 'submitted':
      return null
    case 'answered':
      return <AnsweredQuestions answers={state.answers} />
    case 'not_answered':
      return <QuestionNote text="没有回答这些问题" />
  }
}

/**
 * 停下来等用户批准的工具调用：显示调用的工具与参数，待批准时给出拒绝、始终允许、批准三个按钮（始终允许：以后同样的调用
 * 不再问，设置页可撤销）
 */
function ApprovalBlock({ block, ctx }: { block: ApprovalBlockData; ctx: SessionContext }) {
  const { busy, error, run } = useAction()
  // 失败（如新消息先把这次调用关掉了，409）要让人看到，不吞掉
  const decide = (choice: ApprovalChoice) => void run(() => ctx.approve(block.callId, choice))
  return (
    // 等待项里的「去批准」滚到这里
    <div
      id={approvalAnchor(block.callId)}
      className="flex scroll-mt-4 flex-col gap-2 self-stretch rounded-xl border border-warning/40 bg-surface p-3 shadow-sm"
    >
      <div className="flex items-center gap-1.5 text-sm">
        <ShieldQuestion className="size-4 shrink-0 text-warning" />
        <span>{block.status === 'pending' ? '这次调用要你批准才执行' : '已提交决定'}</span>
        <span className="font-mono text-muted">{block.tool.name}</span>
      </div>
      <ToolDetail tool={block.tool} />
      <ErrorAlert error={error} />
      {block.status === 'pending' && (
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="secondary" isDisabled={busy} onPress={() => decide('deny')}>
            拒绝
          </Button>
          <Button size="sm" variant="secondary" isDisabled={busy} onPress={() => decide('always')}>
            始终允许
          </Button>
          <Button size="sm" isDisabled={busy} onPress={() => decide('approve')}>
            批准
          </Button>
        </div>
      )}
    </div>
  )
}

/** 事件来源的可读名称：thread 显示名字与 agent（可点开），后台任务显示编号 */
function SourceLabel({ source, ctx }: { source: ItemSource; ctx: SessionContext }) {
  switch (source.kind) {
    case 'job':
      return <span className="shrink-0 text-foreground">后台任务 #{source.job_id}</span>
    case 'wake':
      return <span className="shrink-0 text-foreground">到点唤醒</span>
    case 'thread': {
      const thread = ctx.threads.get(source.session_id)
      if (!thread && ctx.view === 'history') return <span className="shrink-0">thread（已删除）</span>
      return (
        <Link to={sessionRoute(source.session_id)} className="flex min-w-0 shrink items-center gap-1 text-accent hover:underline">
          <GitBranch className="size-3.5 shrink-0" />
          <span className="truncate">
            {thread ? <ThreadName thread={thread} /> : 'thread'}
          </span>
        </Link>
      )
    }
  }
}

/** 会话事件（后台任务完成、thread 交回结果……）：不是用户说的话，单独显示，默认收起内容 */
function EventNote({ lead, text }: { lead: ReactNode; text: string }) {
  return (
    <Collapsible
      lead={
        <>
          <BellDot className="size-3.5 shrink-0" />
          {lead}
        </>
      }
      title={<span className="truncate">{text.split('\n', 1)[0]}</span>}
    >
      <pre className={CODE_BLOCK}>{text}</pre>
    </Collapsible>
  )
}

/** 同一来源连续的运行中检查（blocks.foldCheckIns）：一行汇总，展开看每次检查 */
function CheckInsNote({ source, checks, ctx }: { source: ItemSource; checks: CheckIn[]; ctx: SessionContext }) {
  const last = checks[checks.length - 1].at
  return (
    <Collapsible
      lead={
        <>
          <BellDot className="size-3.5 shrink-0" />
          <SourceLabel source={source} ctx={ctx} />
        </>
      }
      title={
        <span className="truncate">
          仍在进行 · 已检查 {checks.length} 次{last && ` · 最近 ${formatTime(last)}`}
        </span>
      }
    >
      <div className="flex flex-col gap-2">
        {checks.map((c, i) => (
          <div key={i} className="flex flex-col gap-1">
            {c.at && <span className="text-xs">{formatTime(c.at)}</span>}
            <pre className={CODE_BLOCK}>{c.text}</pre>
          </div>
        ))}
      </div>
    </Collapsible>
  )
}

function Blocks({ items, ctx }: { items: Item[]; ctx: SessionContext }) {
  return (
    <>
      {foldCheckIns(groupSteps(toBlocks(items))).map((v, i) => {
        switch (v.kind) {
          case 'ask':
            // 表单、提交状态跟着这次调用走：前面多出或少了块时，不串到别的提问、审批上
            return <AskBlock key={`ask:${v.ask.callId}`} block={v.ask} ctx={ctx} />
          case 'approval':
            return <ApprovalBlock key={`approval:${v.approval.callId}`} block={v.approval} ctx={ctx} />
          case 'stopped':
            return (
              <div key={i} className="flex items-center gap-1.5 self-start text-xs text-muted">
                <CircleStop className="size-3.5" />
                已停止
              </div>
            )
          case 'user':
            return <UserBubble key={i} text={v.text} attachments={v.attachments} />
          case 'event':
            return <EventNote key={i} lead={<SourceLabel source={v.source} ctx={ctx} />} text={v.text} />
          case 'checks':
            return <CheckInsNote key={i} source={v.source} checks={v.checks} ctx={ctx} />
          case 'assistant':
            return (
              <div key={i} className="max-w-[88%] self-start rounded-2xl rounded-bl-md bg-surface px-3.5 py-2.5 shadow-sm">
                <Markdown text={v.text} />
              </div>
            )
          case 'process':
            return <StepsGroup key={i} steps={v.steps} ctx={ctx} />
        }
      })}
    </>
  )
}

/** 按压缩点切分历史：每个压缩点之前的消息已由摘要代替发给模型（切点总在两轮之间）。 */
function History({ items, compactions, ctx }: { items: SavedItem[]; compactions: CompactionInfo[]; ctx: SessionContext }) {
  const parts: ReactNode[] = []
  let rest = items
  for (const c of compactions) {
    const cut = rest.findIndex((i) => i.message_id > c.through_message_id)
    const before = cut === -1 ? rest : rest.slice(0, cut)
    rest = cut === -1 ? [] : rest.slice(cut)
    parts.push(<Blocks key={`b${c.through_message_id}`} items={before.map((i) => i.item)} ctx={ctx} />)
    parts.push(
      <Disclosure key={`c${c.through_message_id}`} className="border-y border-dashed border-separator py-1.5 text-sm text-muted">
        <Disclosure.Heading>
          <Disclosure.Trigger className="flex w-full items-center justify-center gap-2">
            以上内容已压缩 · {formatTime(c.created_at)}
            <Disclosure.Indicator />
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="pt-2">
            <Markdown text={c.summary} />
          </Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>,
    )
  }
  parts.push(<Blocks key="rest" items={rest.map((i) => i.item)} ctx={ctx} />)
  return <>{parts}</>
}

/** 显示在历史之后的信件：排队中的显示为待处理，最近一次停止丢弃的划掉 */
function MailNote({ mail, ctx }: { mail: Mail; ctx: SessionContext }) {
  const discarded = mail.state === 'discarded'
  return (
    <div className={`flex flex-col gap-1.5 ${discarded ? 'line-through opacity-50' : ''}`}>
      {mail.kind === 'event' ? (
        mail.check_in ? (
          <CheckInsNote source={mail.source} checks={[{ text: mail.content, at: mail.created_at }]} ctx={ctx} />
        ) : (
          <EventNote lead={<SourceLabel source={mail.source} ctx={ctx} />} text={mail.content} />
        )
      ) : mail.kind === 'answer' ? (
        mail.result.tool === 'ask_user' ? (
          <AnsweredQuestions answers={mail.result.result.answers} />
        ) : mail.result.tool === 'approval' ? (
          <QuestionNote text={CHOICE_TEXT[mail.result.result.choice]} />
        ) : (
          <EventNote lead={<span className="shrink-0 text-foreground">agent 的回答</span>} text={mail.result.result.text} />
        )
      ) : (
        <UserBubble text={mail.content} attachments={mail.attachments} pending={!discarded} />
      )}
    </div>
  )
}

/** 运行中的提示：出过暂时性的错时说在重试；下次尝试的时间已过（正在重试）就不写时间 */
function retryText(retrying: Retrying | null): string {
  if (retrying === null) return '运行中…'
  if (Date.parse(retrying.next_at) <= Date.now()) return '出错了，正在重试…'
  return `出错了，自动重试中（下次 ${formatTime(retrying.next_at)}）`
}

/** 最近一次运行还没正常结束：在跑（出过暂时性的错时显示它与下次尝试的时间），或最终失败（可重试）。手动压缩不算在内（接口不给） */
function RunStatus({ run, retrying, onRetry }: { run: RunRecord; retrying: Retrying | null; onRetry: () => void }) {
  const { state } = run
  if (state.outcome === 'failed') {
    return (
      <div className="flex flex-col gap-2 rounded-xl bg-danger-soft p-3 text-sm text-danger-soft-foreground">
        <div>运行失败了（{formatTime(state.ended_at)}）</div>
        <pre className={CODE_BLOCK}>{state.error}</pre>
        <div className="text-xs">如果是上下文过长，先点右上角「压缩」，完成后再重试。重试从失败的那一步继续。</div>
        <Button size="sm" variant="secondary" className="self-start" onPress={onRetry}>
          <RotateCcw className="size-4" />
          重试
        </Button>
      </div>
    )
  }
  if (state.outcome !== 'unfinished') return null
  return (
    <div className="flex flex-col items-end gap-1 self-end text-xs text-muted">
      <div className="flex items-center gap-2">
        <Spinner size="sm" /> {retryText(retrying)}
      </div>
      {retrying && <div className="max-w-full break-all text-danger">上次错误：{retrying.error}</div>}
    </div>
  )
}

/** thread 名与做它的 agent */
function ThreadName({ thread }: { thread: Thread }) {
  return (
    <>
      {thread.thread}（<AgentName id={thread.agent_id} />）
    </>
  )
}

function ThreadRow({ thread }: { thread: Thread }) {
  return (
    <Link to={sessionRoute(thread.id)} className="flex items-center gap-2 rounded-lg px-2 py-1 hover:bg-surface-hover">
      <GitBranch className="size-3.5 shrink-0 text-muted" />
      <span className="truncate font-medium">{thread.thread}</span>
      <span className="truncate text-muted">
        <AgentName id={thread.agent_id} />
      </span>
      {thread.unread && <span className="size-2 shrink-0 rounded-full bg-accent" aria-label="未读" />}
      <span className="ml-auto shrink-0 text-xs text-muted">{formatTime(thread.updated_at)}</span>
    </Link>
  )
}

/** 一组步骤里委派开出或续写的 thread */
function ThreadLinks({ ids, ctx }: { ids: SessionId[]; ctx: SessionContext }) {
  if (ids.length === 0) return null
  return (
    <div className="flex w-full max-w-[88%] flex-col self-start rounded-xl border border-separator px-1.5 py-1 text-sm">
      {ids.map((id) => {
        const thread = ctx.threads.get(id)
        // 调用条目上的 thread 与 thread 列表在同一次读取里给出，关联随 thread 删除：拿不到是前后端不一致
        if (!thread) throw new Error(`thread ${id} linked to a call is not among the session's threads`)
        return <ThreadRow key={id} thread={thread} />
      })}
    </div>
  )
}

/** 没有被任何条目引用的 thread（见 blocks.unlinkedThreads）：只能统一列在这里 */
function UnlinkedThreads({ threads }: { threads: Thread[] }) {
  if (threads.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-separator p-3 text-sm">
      <div className="text-muted">未关联到调用的 thread</div>
      {threads.map((t) => (
        <ThreadRow key={t.id} thread={t} />
      ))}
    </div>
  )
}

/** 等待项上的操作：结束后台任务、停止 thread（经页面的 action，出错显示在输入框上方） */
interface WaitActions {
  agentId: AgentId
  run: (fn: () => Promise<unknown>) => void
  busy: boolean
}

function WaitRow({ wait, now, ctx, actions }: { wait: Wait; now: number; ctx: SessionContext; actions: WaitActions }) {
  const detail = <span className="text-muted">{waitDetail(wait, now)}</span>
  switch (wait.kind) {
    case 'job': {
      const job = wait.job
      return (
        <Collapsible
          lead={
            <>
              <SquareTerminal className="size-3.5 shrink-0" />
              <span className="shrink-0 text-foreground">后台任务 #{wait.job_id}</span>
            </>
          }
          title={
            <span className="flex min-w-0 gap-2">
              <span className="truncate font-mono">{job.command.split('\n', 1)[0]}</span>
              <span className="shrink-0">{waitDetail(wait, now)}</span>
            </span>
          }
        >
          <div className="flex flex-col gap-2">
            <pre className={CODE_BLOCK}>{job.command}</pre>
            <div className="text-xs text-muted">日志 {job.log_path}（在 agent 工作目录里）最后几行：</div>
            <pre className={CODE_BLOCK}>{job.tail || '（还没有输出）'}</pre>
            {job.status !== 'running' ? (
              <div className="text-xs text-muted">已结束，等结局交回会话</div>
            ) : (
              <Button
                size="sm"
                variant="danger-soft"
                className="self-start"
                isDisabled={actions.busy}
                onPress={() => actions.run(() => api.killJob(actions.agentId, wait.job_id))}
              >
                <Square className="size-3.5" />
                结束任务
              </Button>
            )}
          </div>
        </Collapsible>
      )
    }
    case 'delegation': {
      const id = wait.thread_session_id
      const thread = ctx.threads.get(id)
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <GitBranch className="size-3.5 shrink-0 text-muted" />
          <Link to={sessionRoute(id)} className="max-w-full truncate text-accent hover:underline">
            {thread ? <ThreadName thread={thread} /> : 'thread'}
          </Link>
          {detail}
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto shrink-0"
            isDisabled={actions.busy}
            onPress={() => actions.run(() => api.stop(id))}
            aria-description="停止做这项委派的 thread，结局以「被停止」交回"
          >
            <Square className="size-3.5" />
            停止 thread
          </Button>
        </div>
      )
    }
    case 'wake':
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5" title={wait.note}>
          <AlarmClock className="size-3.5 shrink-0 text-muted" />
          <span className="shrink-0">到点唤醒 {detail}</span>
          <span className="max-w-full truncate text-muted">{wait.note}</span>
        </div>
      )
    case 'ask':
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <MessageCircleQuestion className="size-3.5 shrink-0 text-muted" />
          <span className="shrink-0">等你回答</span>
          {detail}
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto shrink-0"
            onPress={() => document.getElementById(askAnchor(wait.call_id))?.scrollIntoView({ behavior: 'smooth' })}
          >
            跳到提问
          </Button>
        </div>
      )
    case 'approval':
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <ShieldQuestion className="size-3.5 shrink-0 text-muted" />
          <span className="shrink-0">等你批准</span>
          <span className="font-mono">{detail}</span>
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto shrink-0"
            onPress={() => document.getElementById(approvalAnchor(wait.call_id))?.scrollIntoView({ behavior: 'smooth' })}
          >
            去批准
          </Button>
        </div>
      )
  }
}

/** 会话在等的东西（与工作状态同一条规则）：让人看得出它为什么挂起、在等谁 */
function WaitsPanel({ waits, ctx, actions }: { waits: Wait[]; ctx: SessionContext; actions: WaitActions }) {
  const now = useNow(waits.length > 0)
  if (waits.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-dashed border-separator p-3 text-sm">
      <div className="flex items-center gap-1.5 text-muted">
        <Hourglass className="size-3.5" />
        等待中
      </div>
      {waits.map((w) => (
        <WaitRow key={waitKey(w)} wait={w} now={now} ctx={ctx} actions={actions} />
      ))}
    </div>
  )
}

/** 有文字或附件即可发送；附件还在上传或有失败的未移除时不能发送 */
const canSend = (draft: string, uploads: Uploads) => (draft.trim() !== '' || uploads.ids.length > 0) && !uploads.blocking

function Composer({
  draft,
  setDraft,
  uploads,
  voice,
  busy,
  active,
  onSend,
  onStop,
}: {
  draft: string
  setDraft: (v: string) => void
  uploads: Uploads
  voice: VoiceInput
  busy: boolean
  /** 这个会话有事在进行（工作状态不是 idle：运行、排队、等后台任务或委派；或在手动压缩）：显示停止按钮（进行中也可以继续发消息，会排在后面） */
  active: boolean
  onSend: () => void
  onStop: () => void
}) {
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (isSendShortcut(e)) {
      e.preventDefault()
      onSend()
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <PendingUploads uploads={uploads} />
      <div className="flex items-end gap-2">
        <AttachButton uploads={uploads} isDisabled={busy} />
        <VoiceButton voice={voice} isDisabled={busy} />
        <TextField value={draft} onChange={setDraft} aria-label="回复" variant="secondary" className="flex-1">
          <TextArea
            placeholder={`回复…（${SEND_HINT}）`}
            rows={Math.min(6, Math.max(1, draft.split('\n').length))}
            onKeyDown={onKeyDown}
            onPaste={uploads.onPaste}
            className="resize-none"
          />
        </TextField>
        {active && (
          <Button isIconOnly variant="danger-soft" isDisabled={busy} onPress={onStop} aria-label="停止">
            <Square className="size-4" />
          </Button>
        )}
        <Button isIconOnly isDisabled={busy || !canSend(draft, uploads)} onPress={onSend} aria-label="发送">
          <SendHorizontal className="size-4" />
        </Button>
      </div>
    </div>
  )
}

// 离底部这么近仍算「在底部」
const PINNED_SLACK_PX = 50

/**
 * 新内容到来时只在用户本来就在底部时跟随；往上翻看历史时不把人拽下来。
 *
 * 「在不在底部」只有一个来源 pinned，除 pin() 外只由用户的滚动改变：滚到离底部 50px 以内就钉住，向上滚离开就解除。不能每次
 * scroll 事件都按距底距离重算——程序滚到底产生的 scroll 事件要到下一帧才派发，那时内容可能又长了，会被误判成
 * 「离开了底部」；而内容变长时 scrollTop 只会不变或变大，不会被当成向上滚。高度变化（ResizeObserver：新数据、
 * 流式输出、图片加载完、输入框变高使容器变矮）时 pinned 为真就滚到底；内容变短时 scrollTop 被钳到最底，距底为 0，
 * 也不会被当成向上滚。打开会话、发送消息就是重新钉住。
 * 代价：在底部展开最后一个折叠块时，页面会跟着滚到它的末尾。
 */
function useFollowBottom() {
  const scroller = useRef<HTMLElement | null>(null)
  const pinned = useRef(true)
  // null：内容没挂载（加载中），不显示「回到底部」
  const [atBottom, setAtBottom] = useState<boolean | null>(null)
  const content = useRef<HTMLElement | null>(null)
  // 上一次看到的 scrollTop：比它小才是向上滚
  const lastTop = useRef(0)

  const toEnd = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    el.scrollTop = el.scrollHeight
    lastTop.current = el.scrollTop
  }, [])
  const onScroll = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    if (el.scrollHeight - el.scrollTop - el.clientHeight <= PINNED_SLACK_PX) pinned.current = true
    else if (el.scrollTop < lastTop.current) pinned.current = false
    lastTop.current = el.scrollTop
    setAtBottom(pinned.current)
  }, [])
  // 观察滚动容器（输入框变高时它变矮）与内容（新数据、流式输出、图片加载）的尺寸；两个 ref 各自登记，不依赖挂载先后
  const observer = useRef<ResizeObserver | null>(null)
  const resize = useCallback(
    () =>
      (observer.current ??= new ResizeObserver(() => {
        if (pinned.current) toEnd()
      })),
    [toEnd],
  )
  useEffect(() => () => observer.current?.disconnect(), [])

  const scrollRef = useCallback(
    (el: HTMLElement | null) => {
      if (scroller.current !== null) {
        scroller.current.removeEventListener('scroll', onScroll)
        resize().unobserve(scroller.current)
      }
      scroller.current = el
      if (el !== null) {
        el.addEventListener('scroll', onScroll, { passive: true })
        resize().observe(el)
      }
    },
    [onScroll, resize],
  )
  // 内容末尾的锚点：数据加载后才出现，观察它所在的内容容器
  const contentEndRef = useCallback(
    (el: HTMLDivElement | null) => {
      if (content.current !== null) resize().unobserve(content.current)
      content.current = el?.parentElement ?? null
      if (content.current === null) {
        setAtBottom(null)
        return
      }
      resize().observe(content.current)
      setAtBottom(pinned.current)
    },
    [resize],
  )
  /** 回到底部并跟随之后的新内容（打开会话、发送消息、点「回到底部」） */
  const pin = useCallback(() => {
    pinned.current = true
    setAtBottom(true)
    toEnd()
  }, [toEnd])
  return { scrollRef, contentEndRef, atBottom, pin }
}

/**
 * 输入框上方的一行小字（不随内容滚动：会话从底部看起，运行中也看得到）：发给模型的上下文现在多大、到多大会压缩，
 * 以及这个会话累计花掉的 token（输入已含缓存读写）。两者都随每一步保存更新：每次模型请求完成就记账，
 * 运行中只差正在进行的那一次请求
 */
function UsageLine({ context, usage }: Pick<SessionDetail, 'context' | 'usage'>) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 px-1 text-xs text-muted">
      <span title="发给模型的上下文：现在的大小 / 压缩阈值（超过就压缩）">
        上下文 {contextText(context.tokens, context.threshold_tokens)}
      </span>
      <span title="这个会话累计花掉的 token（每次模型请求完成就记上）">
        累计 输入 {formatTokens(usage.input_tokens)} · 输出 {formatTokens(usage.output_tokens)} · 缓存命中{' '}
        {cacheHitPercent(usage.input_tokens, usage.cache_read_tokens)}% · {usage.requests} 次请求
      </span>
    </div>
  )
}

/** 会话页按哪些事件重新拉取：连上了（之前可能丢了事件），或这个会话的历史或运行状态变了（saved：又保存了一步，
 * 或运行、压缩开始、结束、失败、停止、重试） */
function refetchesSession(e: PageEvent, id: SessionId): boolean {
  if (e.type === 'connected') return true
  return e.type === 'saved' && e.session_id === id
}

/** 用户看过了这个会话：指向它的通知标为已读（会话的未读由通知派生），标记本身幂等 */
function markSessionRead(id: SessionId) {
  void api.markRead(id).catch((e) => console.error('marking session read failed', e))
}

export default function SessionView() {
  const id = asSessionId(useRouteParam('id'))
  const navigate = useNavigate()
  const confirm = useConfirm()
  // 整份会话（含全部历史，大的有几 MB）不随任何会话的 changed 重新拉取：只按这个会话自己的事件刷新（连上了、saved），
  // 用户的操作之后照常一起刷新（load.refreshShown）
  const detail = useLoad(() => api.session(id), [id], (e) => refetchesSession(e, id))
  const [live, setLive] = useState<RunEvent[]>([])
  const [draft, setDraft] = useState('')
  const uploads = useUploads()
  const action = useAction()
  // 压缩单独一个：它要做完才返回（几秒到几分钟），期间停止等其他操作照常可用
  const compaction = useAction()
  // 压缩中用户点了停止：压缩请求随后以「已停止」结束，那是用户要的结果，不当错误显示
  const stoppedCompaction = useRef(false)
  const bottom = useFollowBottom()
  // 说完就发：转写出的文字接在已输入的内容后面，连同附件一起发出；暂时发不了（附件还在上传、上一条还在发）
  // 或发送失败时留在输入框里，不丢
  const voice = useVoiceInput((text) => {
    const content = appendTranscript(draft, text)
    if (action.busy || !canSend(content, uploads)) setDraft(content)
    else void send(content).then((ok) => ok || setDraft(content))
  })

  // 实时流与已读：重新拉取由上面的 Loader 负责（又保存了一步：新快照换成新的一段，合并时丢掉上一段的事件）
  useServerEvents((e) => {
    if (e.type === 'connected') setLive([])
    else if (e.type !== 'changed' && e.session_id === id) {
      if (e.type === 'run') setLive((prev) => [...withoutCovered(detail.data?.live ?? null, prev, e), e])
      // 看着它时又来了指向它的通知（会话的未读由通知派生）：用户已经在看，直接标记已读
      else if (e.type === 'notification') markSessionRead(id)
    }
  })

  // 重新拉取到的快照里没有进行中的运行（运行或压缩结束了）：以数据库为准，清空临时流。快照生成后才开始的运行，
  // 已收到的事件被清掉时下一条事件接不上（mergeLive 的 gap），会再拉一次；清掉到下一条事件之间画面暂时缺这几条，是预期的
  // （渲染时按「快照换了」调整，React 推荐的做法，不用 effect）
  const [snapshot, setSnapshot] = useState(detail.data)
  if (detail.data !== snapshot) {
    setSnapshot(detail.data)
    if (detail.data?.live === null) setLive([])
  }

  // 中途打开或重新拉取时以快照为准，从这一段的开头显示，再接上经 SSE 收到、快照里还没有的事件
  const merged = mergeLive(detail.data?.live ?? null, live)
  const shownLive = merged.events
  const reload = detail.reload
  useEffect(() => {
    if (merged.gap) void reload()
  }, [merged.gap, reload])

  // 打开会话即标记已读（看着它时新来的通知见上面的事件处理）
  useEffect(() => markSessionRead(id), [id])

  // 打开会话时从底部看起（每个会话一个页面实例，只在挂载时）
  const { pin } = bottom
  useEffect(() => pin(), [pin])

  /** content：要发的文字，默认是输入框里的；语音输入时是输入框内容接上转写结果 */
  async function send(content: string = draft) {
    // 快捷键不经过按钮的禁用状态：请求进行中再按一次不能重复发送
    if (action.busy || !canSend(content, uploads)) return false
    const sent = uploads.batch()
    const shown = draft
    const ok = await action.run(() => api.reply(id, content.trim(), sent.ids))
    if (ok) {
      // 自己发的消息总要看到
      pin()
      // 发送过程中又改了输入框就保留新内容
      setDraft((current) => (current === shown ? '' : current))
      uploads.removeMany(sent.keys)
    }
    return ok
  }

  async function remove() {
    const ok = await confirm({
      title: '删除这个会话？',
      body: '会话的全部消息都会被删除；它开出的 thread 保留。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteSession(id), { leave: () => navigate(parentRoute) })
  }

  const session = detail.data?.session
  const parent = session?.origin.kind === 'delegation' ? session.origin.parent_session_id : null
  const parentRoute = parent ? sessionRoute(parent) : '/'
  return (
    <Page
      scrollRef={bottom.scrollRef}
      back={parentRoute}
      title={
        session?.origin.kind === 'issue' ? (
          <IssueTitle issueId={session.origin.issue_id} fallback={session.title} />
        ) : (
          (session?.title ?? '会话')
        )
      }
      actions={
        <>
          <ActionButton
            size="sm"
            variant="secondary"
            isDisabled={action.busy}
            isPending={compaction.busy}
            onPress={() => {
              stoppedCompaction.current = false
              void compaction.run(() =>
                api.compact(id).catch((e: unknown) => {
                  if (stoppedCompaction.current) toast('压缩已停止')
                  else throw e
                }),
              )
            }}
            aria-description="把早期对话压缩成摘要，减少发给模型的上下文"
          >
            {!compaction.busy && <Archive className="size-4" />}
            压缩
          </ActionButton>
          <Button isIconOnly size="sm" variant="ghost" onPress={() => void remove()} aria-label="删除会话">
            <Trash2 className="size-4" />
          </Button>
        </>
      }
      footer={
        // 底部安全区由下面的导航栏留出，这里不重复
        <div className="border-t border-separator bg-surface px-3 pt-2 pb-2">
          <div className="relative mx-auto flex w-full max-w-3xl flex-col gap-2">
            {bottom.atBottom === false && (
              <Button
                isIconOnly
                size="sm"
                variant="secondary"
                className="absolute -top-14 right-0 rounded-full shadow"
                aria-label="回到底部"
                onPress={pin}
              >
                <ArrowDown className="size-4" />
              </Button>
            )}
            <ErrorAlert error={action.error ?? compaction.error ?? voice.error} />
            {detail.data && <UsageLine context={detail.data.context} usage={detail.data.usage} />}
            <Composer
              draft={draft}
              setDraft={setDraft}
              uploads={uploads}
              voice={voice}
              busy={action.busy}
              // 压缩中也能停止（会话此时是空闲的）
              active={compaction.busy || (detail.data !== undefined && detail.data.state !== 'idle')}
              onSend={() => void send()}
              onStop={() => {
                if (compaction.busy) stoppedCompaction.current = true
                void action.run(() => api.stop(id))
              }}
            />
          </div>
        </div>
      }
    >
      <Loading state={detail}>
        {(d) => {
          const ctx: SessionContext = {
            answer: (callId, answers) => api.answer(id, callId, answers),
            approve: (callId, choice) => api.approve(id, callId, choice),
            threads: new Map(d.threads.map((t) => [t.id, t])),
            view: 'history',
          }
          return (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-center gap-2">
                <Link to={agentRoute(d.session.agent_id)}>
                  <Chip size="sm" color="accent" variant="soft">
                    <AgentName id={d.session.agent_id} />
                  </Chip>
                </Link>
                <span className="text-xs text-muted">{formatTime(d.session.created_at)}</span>
              </div>
              {d.session.origin.kind === 'delegation' && parent && (
                <Link to={parentRoute} className="flex items-center justify-center gap-1 text-xs text-accent">
                  <CornerLeftUp className="size-3.5" />
                  来自上级会话的 thread「{d.session.origin.thread}」
                </Link>
              )}
              <History items={d.items} compactions={d.compactions} ctx={ctx} />
              {d.mail.map((m) => (
                <MailNote key={m.id} mail={m} ctx={ctx} />
              ))}
              {d.run && <RunStatus run={d.run} retrying={d.retrying} onRetry={() => void action.run(() => api.retry(id))} />}
              {shownLive.length > 0 && (
                <div className="flex flex-col gap-3 opacity-90">
                  <Blocks items={liveItems(shownLive)} ctx={{ ...ctx, view: 'live' }} />
                </div>
              )}
              <WaitsPanel
                waits={d.waits}
                ctx={ctx}
                actions={{
                  agentId: d.session.agent_id,
                  busy: action.busy,
                  // 结束、停止之后占用要等结局交回才释放，那时的运行会让页面重新拉取；操作之后先拉一次显示即时的状态
                  run: (fn) => void action.run(fn),
                }}
              />
              <UnlinkedThreads threads={unlinkedThreads([...d.items.map((i) => i.item), ...liveItems(shownLive)], d.threads)} />
              <div ref={bottom.contentEndRef} aria-hidden />
            </div>
          )
        }}
      </Loading>
    </Page>
  )
}
