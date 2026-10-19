import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, asAgentId, type TaskStatus } from '../api'
import { closeServerEvents } from '../events'
import { enablePush, pushState, type PushState } from '../push'
import { ErrorBanner, Field, Loading, Page, agentLabel, confirmAction, formatTime, useAction, useAgents, useLoad } from '../ui'

export function Vault() {
  const secrets = useLoad(() => api.secrets(), [])
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [description, setDescription] = useState('')
  const action = useAction()

  async function save() {
    if (await action.run(() => api.setSecret(key, value, description))) {
      setKey('')
      setValue('')
      setDescription('')
      await secrets.reload()
    }
  }

  return (
    <Page title="Vault" back="/settings">
      <p className="muted small">
        Secret 加密保存，值不会再显示。agent 通过 key 引用：MCP 配置里写 <code>{'${vault:KEY}'}</code>，shell 里用
        <code>run_shell(secrets=[KEY])</code> 注入同名环境变量。
      </p>
      <div className="card form">
        <Field label="Key">
          <input value={key} onChange={(e) => setKey(e.target.value)} className="mono" autoCapitalize="characters" placeholder="SLACK_TOKEN" />
        </Field>
        <Field label="值">
          <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoComplete="off" />
        </Field>
        <Field label="说明">
          <input value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <ErrorBanner error={action.error} />
        <button className="button primary" onClick={save} disabled={action.busy || !key || !value}>
          保存（同名覆盖）
        </button>
      </div>
      <Loading state={secrets}>
        {(list) => (
          <ul className="list">
            {list.map((s) => (
              <li key={s.key} className="list-item">
                <div className="list-main">
                  <div className="list-title mono">{s.key}</div>
                  <div className="list-sub">
                    {s.description} · {formatTime(s.updated_at)}
                  </div>
                </div>
                <button
                  className="button small danger"
                  onClick={() => {
                    if (confirmAction(`删除 ${s.key}？引用它的 MCP 与命令会失败。`))
                      void action.run(() => api.deleteSecret(s.key).then(secrets.reload))
                  }}
                >
                  删除
                </button>
              </li>
            ))}
          </ul>
        )}
      </Loading>
    </Page>
  )
}

export function Webhooks() {
  const hooks = useLoad(() => api.webhooks(), [])
  const agents = useAgents()
  const [form, setForm] = useState({ name: '', agent_id: asAgentId(''), prompt: '' })
  const action = useAction()

  async function create() {
    if (await action.run(() => api.createWebhook(form))) {
      setForm({ name: '', agent_id: asAgentId(''), prompt: '' })
      await hooks.reload()
    }
  }

  return (
    <Page title="Webhooks" back="/settings">
      <p className="muted small">
        外部系统 POST 到 webhook URL，请求头 <code>X-Muliao-Token</code> 带上 token；请求体会附在提示词后，交给 agent 在新会话中处理，完成后推送通知。
      </p>
      <details className="card">
        <summary>新建 webhook</summary>
        <div className="form">
          <Field label="名称">
            <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </Field>
          <Field label="Agent">
            <select value={form.agent_id} onChange={(e) => setForm({ ...form, agent_id: asAgentId(e.target.value) })}>
              <option value="" disabled>
                选择 agent
              </option>
              {agents.data?.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="处理说明">
            <textarea rows={4} value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} />
          </Field>
          <ErrorBanner error={action.error} />
          <button className="button primary" onClick={create} disabled={action.busy || !form.name || !form.agent_id || !form.prompt}>
            创建
          </button>
        </div>
      </details>
      <Loading state={hooks}>
        {(list) => (
          <ul className="cards">
            {list.map((h) => (
              <li key={h.id} className="card">
                <div className="card-head">
                  <strong>{h.name}</strong>
                  <span className="tag">{agentLabel(agents.data, h.agent_id)}</span>
                </div>
                <code className="block">{`curl -X POST ${h.url} \\\n  -H 'X-Muliao-Token: ${h.token}' \\\n  -d '{"hello":"world"}'`}</code>
                <p className="prewrap">{h.prompt}</p>
                <button
                  className="button small danger"
                  onClick={() => {
                    if (confirmAction(`删除 webhook ${h.name}？`)) void action.run(() => api.deleteWebhook(h.id).then(hooks.reload))
                  }}
                >
                  删除
                </button>
              </li>
            ))}
          </ul>
        )}
      </Loading>
    </Page>
  )
}

const STATUS_FILTERS: { label: string; status?: TaskStatus }[] = [
  { label: '失败', status: 'failed' },
  { label: '等待', status: 'pending' },
  { label: '运行中', status: 'running' },
  { label: '全部' },
]

export function Tasks() {
  const [filter, setFilter] = useState(0)
  const tasks = useLoad(() => api.tasks(STATUS_FILTERS[filter].status), [filter])
  const agents = useAgents()
  const action = useAction()
  return (
    <Page title="任务" back="/settings">
      <div className="segmented wide">
        {STATUS_FILTERS.map((f, i) => (
          <button key={f.label} className={i === filter ? 'active' : ''} onClick={() => setFilter(i)}>
            {f.label}
          </button>
        ))}
      </div>
      <ErrorBanner error={action.error} />
      <Loading state={tasks}>
        {(list) => (
          <ul className="cards">
            {list.map((t) => (
              <li key={t.id} className="card">
                <div className="card-head">
                  <strong>#{t.id}</strong>
                  <span className="tag">{t.kind}</span>
                  {t.agent_id && <span className="tag subtle">{agentLabel(agents.data, t.agent_id)}</span>}
                  <span className={`tag status-${t.status}`}>{t.status}</span>
                </div>
                <div className="muted small">
                  {formatTime(t.created_at)} · 尝试 {t.attempts} 次
                  {t.session_id && (
                    <>
                      {' '}· <Link to={`/sessions/${t.session_id}`}>会话</Link>
                    </>
                  )}
                </div>
                {t.prompt && <p className="prewrap clamp">{t.prompt}</p>}
                {t.error && <pre className="error-text">{t.error}</pre>}
                {t.status === 'failed' && (
                  <button className="button small" onClick={() => void action.run(() => api.retryTask(t.id).then(tasks.reload))}>
                    重试
                  </button>
                )}
              </li>
            ))}
            {list.length === 0 && <div className="muted center">没有任务</div>}
          </ul>
        )}
      </Loading>
    </Page>
  )
}

const PUSH_TEXT: Record<PushState, string> = {
  unsupported: '此浏览器不支持推送通知。',
  'needs-install': 'iOS 需要先用 Safari「分享 → 添加到主屏幕」，再从主屏幕图标打开本应用，才能开启推送。',
  default: '尚未开启推送。',
  denied: '通知权限已被拒绝，请在系统设置中为本应用开启通知。',
  granted: '推送已开启。',
}

export function Settings() {
  const navigate = useNavigate()
  const [push, setPush] = useState<PushState>(pushState())
  const action = useAction()

  async function logout() {
    await action.run(async () => {
      await api.logout()
      closeServerEvents()
      navigate('/login')
    })
  }

  return (
    <Page title="设置">
      <section className="card">
        <h2>推送通知</h2>
        <p className="muted">{PUSH_TEXT[push]}</p>
        <ErrorBanner error={action.error} />
        <div className="row">
          {(push === 'default' || push === 'granted') && (
            <button
              className="button primary small"
              disabled={action.busy}
              onClick={() => void action.run(enablePush).then(() => setPush(pushState()))}
            >
              {push === 'granted' ? '重新订阅本设备' : '开启推送'}
            </button>
          )}
          {push === 'granted' && (
            <button className="button small" disabled={action.busy} onClick={() => void action.run(api.testPush)}>
              发送测试通知
            </button>
          )}
        </div>
      </section>
      <ul className="list">
        {[
          ['/skills', '公共 Skills'],
          ['/mcp', 'MCP servers'],
          ['/vault', 'Vault'],
          ['/webhooks', 'Webhooks'],
          ['/tasks', '任务与失败记录'],
        ].map(([to, label]) => (
          <li key={to}>
            <Link to={to} className="list-item">
              <div className="list-main">
                <div className="list-title">{label}</div>
              </div>
              <span className="chevron">›</span>
            </Link>
          </li>
        ))}
      </ul>
      <button
        className="button danger"
        onClick={() => {
          if (confirmAction('退出登录？')) void logout()
        }}
      >
        退出登录
      </button>
    </Page>
  )
}

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState('')
  const action = useAction()
  const navigate = useNavigate()

  async function submit() {
    if (await action.run(() => api.login(password))) {
      onLogin()
      navigate('/', { replace: true })
    }
  }

  return (
    <div className="login">
      <img src="/icon-192.png" alt="" width={72} height={72} />
      <h1>Muliao</h1>
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <input type="password" placeholder="密码" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
        <ErrorBanner error={action.error} />
        <button className="button primary" disabled={action.busy || !password}>
          登录
        </button>
      </form>
    </div>
  )
}
