import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, type Agent, type AgentInput } from '../api'
import { ErrorBanner, Field, Loading, Markdown, Page, confirmAction, formatTime, useAction, useLoad } from '../ui'
import { SkillList } from './Skills'

export function AgentList() {
  const agents = useLoad(() => api.agents(), [])
  return (
    <Page
      title="Agents"
      actions={
        <Link className="button primary small" to="/agents/new">
          新建
        </Link>
      }
    >
      <Loading state={agents}>
        {(list) => (
          <ul className="list">
            {list.map((a) => (
              <li key={a.name}>
                <Link to={`/agents/${a.name}`} className="list-item">
                  <div className="list-main">
                    <div className="list-title">{a.name}</div>
                    <div className="list-sub">{a.description}</div>
                  </div>
                  <div className="list-meta">{a.model}</div>
                </Link>
              </li>
            ))}
            {list.length === 0 && <div className="muted center">还没有 agent</div>}
          </ul>
        )}
      </Loading>
    </Page>
  )
}

const EMPTY: AgentInput = { description: '', model: null, instructions: '', reflect_every: null }

function AgentForm({ agent, onSaved }: { agent?: Agent; onSaved: (a: Agent) => void }) {
  const [name, setName] = useState(agent?.name ?? '')
  const [input, setInput] = useState<AgentInput>(
    agent
      ? { description: agent.description, model: agent.model, instructions: agent.instructions, reflect_every: agent.reflect_every }
      : EMPTY,
  )
  const action = useAction()
  const set = (patch: Partial<AgentInput>) => setInput((prev) => ({ ...prev, ...patch }))

  async function save() {
    await action.run(async () => {
      const saved = agent ? await api.updateAgent(agent.name, input) : await api.createAgent(name, input)
      onSaved(saved)
    })
  }

  return (
    <div className="form">
      {!agent && (
        <Field label="名称" hint="小写字母、数字、- 或 _，创建后不可修改">
          <input value={name} onChange={(e) => setName(e.target.value)} autoCapitalize="off" />
        </Field>
      )}
      <Field label="职责描述" hint="其他 agent 委派任务时据此选择">
        <input value={input.description} onChange={(e) => set({ description: e.target.value })} />
      </Field>
      <Field label="模型" hint="<provider 名>:<模型名>，provider 名来自服务端配置的 [llm.providers]，如 claude:claude-opus-5-5；留空用默认模型">
        <input
          value={input.model ?? ''}
          onChange={(e) => set({ model: e.target.value || null })}
          autoCapitalize="off"
          placeholder="服务端默认"
        />
      </Field>
      <Field label="指令（system prompt）">
        <textarea rows={8} value={input.instructions} onChange={(e) => set({ instructions: e.target.value })} />
      </Field>
      <Field label="反思间隔（轮）" hint="每完成这么多轮对话，强制回顾一次是否需要更新 skill 和记忆；留空用服务端默认">
        <input
          type="number"
          min={1}
          value={input.reflect_every ?? ''}
          onChange={(e) => set({ reflect_every: e.target.value ? Number(e.target.value) : null })}
        />
      </Field>
      <ErrorBanner error={action.error} />
      <button className="button primary" onClick={save} disabled={action.busy}>
        保存
      </button>
    </div>
  )
}

export function AgentCreate() {
  const navigate = useNavigate()
  return (
    <Page title="新建 agent" back="/agents">
      <AgentForm onSaved={(a) => navigate(`/agents/${a.name}`, { replace: true })} />
    </Page>
  )
}

function MemoryEditor({ name }: { name: string }) {
  const memory = useLoad(() => api.memory(name), [name])
  const [content, setContent] = useState('')
  const action = useAction()
  useEffect(() => {
    if (memory.data) setContent(memory.data.content)
  }, [memory.data])

  return (
    <Loading state={memory}>
      {(m) => (
        <div className="form">
          <Field label={`记忆（${content.length}/${m.max_chars} 字符）`} hint="agent 每次运行都会看到全部记忆，也会自行修改">
            <textarea rows={14} value={content} onChange={(e) => setContent(e.target.value)} />
          </Field>
          <ErrorBanner error={action.error} />
          <button
            className="button primary"
            disabled={action.busy || content === m.content}
            onClick={() => void action.run(() => api.setMemory(name, content).then(() => memory.reload()))}
          >
            保存记忆
          </button>
        </div>
      )}
    </Loading>
  )
}

function Reflections({ name }: { name: string }) {
  const reflections = useLoad(() => api.reflections(name), [name])
  return (
    <Loading state={reflections}>
      {(list) =>
        list.length === 0 ? (
          <div className="muted center">还没有反思记录</div>
        ) : (
          <ul className="cards">
            {list.map((r) => (
              <li key={r.id} className="card">
                <div className="muted small">
                  {formatTime(r.created_at)} · 消息 #{r.from_message_id + 1}–#{r.to_message_id}
                </div>
                <Markdown text={r.output} />
              </li>
            ))}
          </ul>
        )
      }
    </Loading>
  )
}

const TABS = ['配置', '记忆', 'Skills', '反思'] as const

export function AgentDetail() {
  const { name = '' } = useParams()
  const navigate = useNavigate()
  const agent = useLoad(() => api.agent(name), [name])
  const [tab, setTab] = useState<(typeof TABS)[number]>('配置')
  const action = useAction()

  async function remove() {
    if (!confirmAction(`删除 agent ${name}？它的会话、记忆、私有 skill 和工作目录都会被删除。`)) return
    if (await action.run(() => api.deleteAgent(name))) navigate('/agents')
  }

  return (
    <Page
      title={name}
      back="/agents"
      actions={
        <Link className="button small" to={`/new?agent=${name}`}>
          对话
        </Link>
      }
    >
      <div className="segmented wide">
        {TABS.map((t) => (
          <button key={t} className={t === tab ? 'active' : ''} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </div>
      <Loading state={agent}>
        {(a) => (
          <>
            {tab === '配置' && (
              <>
                <div className="muted small">
                  距下次反思：{a.reflect_every - a.turns_since_reflection} 轮 · 创建于 {formatTime(a.created_at)}
                </div>
                <AgentForm key={a.name} agent={a} onSaved={() => void agent.reload()} />
                <ErrorBanner error={action.error} />
                <button className="button danger" onClick={remove}>
                  删除 agent
                </button>
              </>
            )}
            {tab === '记忆' && <MemoryEditor name={a.name} />}
            {tab === 'Skills' && <SkillList owner={a.name} />}
            {tab === '反思' && <Reflections name={a.name} />}
          </>
        )}
      </Loading>
    </Page>
  )
}
