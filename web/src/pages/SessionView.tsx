import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Button, Chip, Disclosure, Spinner, TextArea, TextField } from '@heroui/react'
import { Archive, RotateCcw, SendHorizontal, Trash2 } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { agentRoute, api, type CompactionInfo, type Item, type Task } from '../api'
import { useServerEvents, type RunEvent } from '../events'
import { CODE_BLOCK, ErrorAlert, Loading, Markdown, Page, agentLabel, formatTime, useAction, useAgents, useConfirm, useLoad } from '../ui'

interface ToolView {
  name: string
  args: string
  result: string | null
  failed: boolean
}

type Block =
  | { kind: 'user'; text: string }
  | { kind: 'assistant'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'tool'; tool: ToolView }

/** 把扁平条目整理成展示块：工具调用与其结果（按 tool_call_id）合并为一块。 */
function toBlocks(items: Item[]): Block[] {
  const blocks: Block[] = []
  const tools = new Map<string, ToolView>()
  for (const item of items) {
    switch (item.type) {
      case 'user':
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

function liveToItems(events: RunEvent[]): Item[] {
  const items: Item[] = []
  for (const e of events) {
    const last = items[items.length - 1]
    if (e.event === 'text') {
      if (last?.type === 'assistant') last.text += e.text
      else items.push({ type: 'assistant', text: e.text, tool_name: null, tool_call_id: null, message_id: null })
    } else {
      items.push({
        type: e.event,
        text: e.text,
        tool_name: e.tool_name ?? null,
        tool_call_id: e.tool_call_id ?? null,
        message_id: null,
      })
    }
  }
  return items
}

function Collapsible({ title, tone = 'default', children }: { title: ReactNode; tone?: 'default' | 'danger'; children: ReactNode }) {
  return (
    <Disclosure
      className={`max-w-[88%] self-start rounded-xl border border-dashed px-3 py-1.5 text-sm text-muted ${
        tone === 'danger' ? 'border-danger' : 'border-separator'
      }`}
    >
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 text-left">
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

function ToolBlock({ tool }: { tool: ToolView }) {
  const status = tool.result === null ? '运行中' : tool.failed ? '出错' : '完成'
  return (
    <Collapsible
      tone={tool.failed ? 'danger' : 'default'}
      title={
        <>
          <span className="font-mono text-foreground">{tool.name}</span>
          <span className="ml-auto text-xs">{status}</span>
        </>
      }
    >
      {tool.args && tool.args !== '{}' && <pre className={CODE_BLOCK}>{tool.args}</pre>}
      {tool.result !== null && <pre className={CODE_BLOCK}>{tool.result}</pre>}
    </Collapsible>
  )
}

function UserBubble({ text, pending }: { text: string; pending?: boolean }) {
  return (
    <div
      className={`max-w-[88%] self-end whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent px-3.5 py-2.5 text-accent-foreground ${
        pending ? 'opacity-70' : ''
      }`}
    >
      {text}
    </div>
  )
}

function Blocks({ items }: { items: Item[] }) {
  return (
    <>
      {toBlocks(items).map((b, i) => {
        switch (b.kind) {
          case 'user':
            return <UserBubble key={i} text={b.text} />
          case 'assistant':
            return (
              <div key={i} className="max-w-[88%] self-start rounded-2xl rounded-bl-md bg-surface px-3.5 py-2.5 shadow-sm">
                <Markdown text={b.text} />
              </div>
            )
          case 'thinking':
            return (
              <Collapsible key={i} title="思考">
                <div className="whitespace-pre-wrap">{b.text}</div>
              </Collapsible>
            )
          case 'tool':
            return <ToolBlock key={i} tool={b.tool} />
        }
      })}
    </>
  )
}

/** 按压缩点切分历史：每个压缩点之前的消息已由摘要代替发给模型（切点总在两轮之间）。 */
function History({ items, compactions }: { items: Item[]; compactions: CompactionInfo[] }) {
  const parts: ReactNode[] = []
  let rest = items
  for (const c of compactions) {
    const cut = rest.findIndex((i) => i.message_id !== null && i.message_id > c.through_message_id)
    const before = cut === -1 ? rest : rest.slice(0, cut)
    rest = cut === -1 ? [] : rest.slice(cut)
    parts.push(<Blocks key={`b${c.through_message_id}`} items={before} />)
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
  parts.push(<Blocks key="rest" items={rest} />)
  return <>{parts}</>
}

function OpenTurn({ task, onRetry }: { task: Task; onRetry: (id: number) => void }) {
  return (
    <>
      <UserBubble text={task.prompt ?? ''} pending />
      {task.status === 'failed' ? (
        <div className="flex flex-col gap-2 rounded-xl bg-danger-soft p-3 text-sm text-danger-soft-foreground">
          <div>这一轮失败了（尝试 {task.attempts} 次）</div>
          <pre className={CODE_BLOCK}>{task.error}</pre>
          <div className="text-xs">如果是上下文过长，先点右上角「压缩」，完成后再重试。</div>
          <Button size="sm" variant="secondary" className="self-start" onPress={() => onRetry(task.id)}>
            <RotateCcw className="size-4" />
            重试
          </Button>
        </div>
      ) : (
        <div className="flex items-center gap-2 self-end text-xs text-muted">
          {task.status === 'running' ? (
            <>
              <Spinner size="sm" /> 处理中…
            </>
          ) : task.attempts > 0 ? (
            `等待重试（${formatTime(task.not_before)}）`
          ) : (
            '排队中…'
          )}
          {task.error && <span>上次错误：{task.error}</span>}
        </div>
      )}
    </>
  )
}

export default function SessionView() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const detail = useLoad(() => api.session(id), [id])
  const agents = useAgents()
  const [live, setLive] = useState<RunEvent[]>([])
  const [draft, setDraft] = useState('')
  const action = useAction()
  const bottom = useRef<HTMLDivElement>(null)

  useServerEvents((e) => {
    if (e.type === 'reconnected') {
      setLive([])
      void detail.reload()
    } else if (e.session_id === id) {
      if (e.type === 'run') setLive((prev) => [...prev, e])
      else if (e.kind === 'turn' || e.kind === 'compaction') {
        // turn 状态变化后以数据库为准重新拉取，清空临时流
        if (e.status !== 'running') setLive([])
        void detail.reload()
      }
    }
  })

  const unread = detail.data?.session.unread
  useEffect(() => {
    if (unread) void api.markRead(id)
  }, [id, unread])

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [detail.data, live.length])

  async function send() {
    const prompt = draft.trim()
    if (!prompt) return
    const ok = await action.run(() => api.reply(id, prompt))
    if (ok) {
      setDraft('')
      await detail.reload()
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // 桌面端 Enter 发送、Shift+Enter 换行；输入法组字时不触发
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && window.matchMedia('(pointer: fine)').matches) {
      e.preventDefault()
      void send()
    }
  }

  async function remove() {
    if (!(await confirm({ title: '删除这个会话？', body: '会话的全部消息都会被删除。', confirmLabel: '删除', danger: true }))) return
    if (await action.run(() => api.deleteSession(id))) navigate('/')
  }

  const session = detail.data?.session
  return (
    <Page
      back="/"
      title={session?.title ?? '会话'}
      actions={
        <>
          <Button
            size="sm"
            variant="secondary"
            isDisabled={action.busy}
            onPress={() => void action.run(() => api.compact(id).then(() => detail.reload()))}
            aria-description="把早期对话压缩成摘要，减少发给模型的上下文"
          >
            <Archive className="size-4" />
            压缩
          </Button>
          <Button isIconOnly size="sm" variant="ghost" onPress={() => void remove()} aria-label="删除会话">
            <Trash2 className="size-4" />
          </Button>
        </>
      }
      footer={
        <div className="border-t border-separator bg-surface px-3 pt-2 pb-[calc(env(safe-area-inset-bottom)+0.5rem)]">
          <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
            <ErrorAlert error={action.error} />
            <div className="flex items-end gap-2">
              <TextField value={draft} onChange={setDraft} aria-label="回复" variant="secondary" className="flex-1">
                <TextArea
                  placeholder="回复…"
                  rows={Math.min(6, Math.max(1, draft.split('\n').length))}
                  onKeyDown={onKeyDown}
                  className="resize-none"
                />
              </TextField>
              <Button isIconOnly isDisabled={action.busy || !draft.trim()} onPress={() => void send()} aria-label="发送">
                <SendHorizontal className="size-4" />
              </Button>
            </div>
          </div>
        </div>
      }
    >
      <Loading state={detail}>
        {(d) => (
          <div className="flex flex-col gap-3">
            <div className="flex items-center justify-center gap-2">
              <Link to={agentRoute(d.session.agent_id)}>
                <Chip size="sm" color="accent" variant="soft">
                  {agentLabel(agents.data, d.session.agent_id)}
                </Chip>
              </Link>
              <span className="text-xs text-muted">{formatTime(d.session.created_at)}</span>
            </div>
            <History items={d.items} compactions={d.compactions} />
            {d.open_turns.map((t) => (
              <OpenTurn key={t.id} task={t} onRetry={(tid) => void action.run(() => api.retryTask(tid)).then(() => detail.reload())} />
            ))}
            {live.length > 0 && (
              <div className="flex flex-col gap-3 opacity-90">
                <Blocks items={liveToItems(live)} />
              </div>
            )}
            <div ref={bottom} />
          </div>
        )}
      </Loading>
    </Page>
  )
}
