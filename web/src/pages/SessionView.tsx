import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type CompactionInfo, type Item, type Task } from '../api'
import { useServerEvents, type RunEvent } from '../events'
import { ErrorBanner, Loading, Markdown, Page, confirmAction, formatTime, useAction, useLoad } from '../ui'

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

function ToolBlock({ tool }: { tool: ToolView }) {
  const status = tool.result === null ? '运行中' : tool.failed ? '出错' : '完成'
  return (
    <details className={`tool ${tool.failed ? 'failed' : ''}`}>
      <summary>
        <span className="tool-name">{tool.name}</span>
        <span className="tool-status">{status}</span>
      </summary>
      {tool.args && tool.args !== '{}' && <pre>{tool.args}</pre>}
      {tool.result !== null && <pre className="tool-result">{tool.result}</pre>}
    </details>
  )
}

function Blocks({ items }: { items: Item[] }) {
  return (
    <>
      {toBlocks(items).map((b, i) => {
        switch (b.kind) {
          case 'user':
            return (
              <div key={i} className="bubble user">
                {b.text}
              </div>
            )
          case 'assistant':
            return (
              <div key={i} className="bubble assistant">
                <Markdown text={b.text} />
              </div>
            )
          case 'thinking':
            return (
              <details key={i} className="thinking">
                <summary>思考</summary>
                <div className="thinking-body">{b.text}</div>
              </details>
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
      <details key={`c${c.through_message_id}`} className="compaction">
        <summary>以上内容已压缩 · {formatTime(c.created_at)}</summary>
        <Markdown text={c.summary} />
      </details>,
    )
  }
  parts.push(<Blocks key="rest" items={rest} />)
  return <>{parts}</>
}

function OpenTurn({ task, onRetry }: { task: Task; onRetry: (id: number) => void }) {
  return (
    <>
      <div className="bubble user pending">{task.prompt}</div>
      {task.status === 'failed' ? (
        <div className="turn-status failed">
          <div>这一轮失败了（尝试 {task.attempts} 次）：</div>
          <pre>{task.error}</pre>
          <div className="small">如果是上下文过长，先点右上角「压缩」，完成后再重试。</div>
          <button className="button small" onClick={() => onRetry(task.id)}>
            重试
          </button>
        </div>
      ) : (
        <div className="turn-status">
          {task.status === 'running' ? '处理中…' : task.attempts > 0 ? `等待重试（${formatTime(task.not_before)}）` : '排队中…'}
          {task.error && <div className="muted small">上次错误：{task.error}</div>}
        </div>
      )}
    </>
  )
}

export default function SessionView() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const detail = useLoad(() => api.session(id), [id])
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
    if (!confirmAction('删除这个会话？')) return
    if (await action.run(() => api.deleteSession(id))) navigate('/')
  }

  const session = detail.data?.session
  return (
    <Page
      back="/"
      title={session?.title ?? '会话'}
      actions={
        <>
          <button
            className="button small"
            disabled={action.busy}
            onClick={() => void action.run(() => api.compact(id).then(() => detail.reload()))}
            title="把早期对话压缩成摘要，减少发给模型的上下文"
          >
            压缩
          </button>
          <button className="icon-button" onClick={remove} aria-label="删除会话">
            🗑
          </button>
        </>
      }
      footer={
      <div className="composer">
          <ErrorBanner error={action.error} />
          <div className="composer-row">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="回复…"
              rows={Math.min(6, Math.max(1, draft.split('\n').length))}
            />
            <button className="button primary" disabled={action.busy || !draft.trim()} onClick={send}>
              发送
            </button>
          </div>
        </div>
      }
    >
      <Loading state={detail}>
        {(d) => (
          <div className="chat">
            <div className="chat-meta">
              <Link to={`/agents/${d.session.agent_name}`} className="tag">
                {d.session.agent_name}
              </Link>
              <span className="muted small">{formatTime(d.session.created_at)}</span>
            </div>
            <History items={d.items} compactions={d.compactions} />
            {d.open_turns.map((t) => (
              <OpenTurn key={t.id} task={t} onRetry={(tid) => void action.run(() => api.retryTask(tid)).then(() => detail.reload())} />
            ))}
            {live.length > 0 && (
              <div className="live">
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
