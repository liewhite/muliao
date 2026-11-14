// 会话的条目：历史、信件、运行状态与运行中的条目怎么画

import { useState, type ReactNode } from 'react'
import { Button, Disclosure, Spinner } from '@heroui/react'
import { BellDot, CircleStop, GitBranch, RotateCcw, ShieldQuestion } from 'lucide-react'
import { Link } from 'react-router-dom'
import type {
  ApprovalChoice,
  Attachment,
  CompactionInfo,
  Item,
  ItemSource,
  Mail,
  Retrying,
  RunRecord,
  SavedItem,
  SessionDetail,
  SessionId,
  Thread,
} from '../api'
import { sessionRoute } from '../routes'
import { AgentName } from '../agents/agentList'
import { CODE_BLOCK, ErrorAlert, Markdown, formatTime, useAction } from '../ui'
import {
  foldCheckIns,
  groupSteps,
  stepAttachments,
  stepThreads,
  summarizeSteps,
  toBlocks,
  toolStatus,
  type ApprovalBlock as ApprovalBlockData,
  type AskBlock as AskBlockData,
  type CheckIn,
  type Step,
  type ToolStatus,
  type ToolView,
} from './blocks'
import { ToolViewBlocks } from './ToolViewBlocks'
import { groupSpan, isRunning, spanLabel, toolSpan, useNow, type Span } from './stepTime'
import { AnsweredQuestions, QuestionForm, QuestionNote } from './questions'
import { AttachmentList } from './attachments'
import { approvalAnchor, askAnchor } from './waits'
import { Collapsible, ThreadName, type SessionContext } from './sessionParts'

// 被退回不是故障：参数不合法或工具报了可纠正的错误，模型会据此修正后重试，所以不用醒目的报错样式
const TOOL_STATUS_LABELS: Record<ToolStatus, string> = {
  running: '运行中',
  unfinished: '未完成',
  rejected: '被退回',
  done: '完成',
}

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
            tool.call && tool.call.args !== null && <pre className={CODE_BLOCK}>{tool.call.args}</pre>
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
      : [unfinished > 0 ? `${unfinished} 次未完成` : '完成', ...(rejected > 0 ? [`${rejected} 次被退回`] : [])].join(
          ' · ',
        )
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
        <Link
          to={sessionRoute(source.session_id)}
          className="flex min-w-0 shrink items-center gap-1 text-accent hover:underline"
        >
          <GitBranch className="size-3.5 shrink-0" />
          <span className="truncate">{thread ? <ThreadName thread={thread} /> : 'thread'}</span>
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
              <div
                key={i}
                className="max-w-[88%] self-start rounded-2xl rounded-bl-md bg-surface px-3.5 py-2.5 shadow-sm"
              >
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
function History({
  items,
  compactions,
  ctx,
}: {
  items: SavedItem[]
  compactions: CompactionInfo[]
  ctx: SessionContext
}) {
  const parts: ReactNode[] = []
  let rest = items
  for (const c of compactions) {
    const cut = rest.findIndex((i) => i.message_id > c.through_message_id)
    const before = cut === -1 ? rest : rest.slice(0, cut)
    rest = cut === -1 ? [] : rest.slice(cut)
    parts.push(<Blocks key={`b${c.through_message_id}`} items={before.map((i) => i.item)} ctx={ctx} />)
    parts.push(
      <Disclosure
        key={`c${c.through_message_id}`}
        className="border-y border-dashed border-separator py-1.5 text-sm text-muted"
      >
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
      <MailContent mail={mail} pending={!discarded} ctx={ctx} />
    </div>
  )
}

function MailContent({ mail, pending, ctx }: { mail: Mail; pending: boolean; ctx: SessionContext }) {
  switch (mail.kind) {
    case 'event':
      return mail.check_in ? (
        <CheckInsNote source={mail.source} checks={[{ text: mail.content, at: mail.created_at }]} ctx={ctx} />
      ) : (
        <EventNote lead={<SourceLabel source={mail.source} ctx={ctx} />} text={mail.content} />
      )
    case 'answer':
      return <AnswerContent result={mail.result} />
    case 'message':
      return <UserBubble text={mail.content} attachments={mail.attachments} pending={pending} />
  }
}

function AnswerContent({ result }: { result: Extract<Mail, { kind: 'answer' }>['result'] }) {
  switch (result.tool) {
    case 'ask_user':
      return <AnsweredQuestions answers={result.result.answers} />
    case 'approval':
      return <QuestionNote text={CHOICE_TEXT[result.result.choice]} />
    case 'ask_agent':
      return (
        <EventNote lead={<span className="shrink-0 text-foreground">agent 的回答</span>} text={result.result.text} />
      )
  }
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
export function UnlinkedThreads({ threads }: { threads: Thread[] }) {
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

/** 会话的全部条目：历史（按压缩点切开）、还没处理的信件、最近一次没正常结束的运行、运行中的条目 */
export function Transcript({
  detail,
  live,
  ctx,
  onRetry,
}: {
  detail: SessionDetail
  /** 运行中的条目（useLiveRun） */
  live: Item[]
  ctx: SessionContext
  onRetry: () => void
}) {
  return (
    <>
      <History items={detail.items} compactions={detail.compactions} ctx={ctx} />
      {detail.mail.map((m) => (
        <MailNote key={m.id} mail={m} ctx={ctx} />
      ))}
      {detail.run && <RunStatus run={detail.run} retrying={detail.retrying} onRetry={onRetry} />}
      {live.length > 0 && (
        <div className="flex flex-col gap-3 opacity-90">
          <Blocks items={live} ctx={{ ...ctx, view: 'live' }} />
        </div>
      )}
    </>
  )
}
