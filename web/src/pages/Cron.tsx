import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, asAgentId, type Agent, type CronJob } from '../api'
import { ErrorBanner, Field, Loading, Page, agentLabel, confirmAction, formatTime, useAction, useAgents, useLoad } from '../ui'

function CronItem({ job, agents, onChange }: { job: CronJob; agents: Agent[] | undefined; onChange: () => void }) {
  const [editing, setEditing] = useState(false)
  const [schedule, setSchedule] = useState(job.schedule)
  const [prompt, setPrompt] = useState(job.prompt)
  const action = useAction()

  const update = (enabled: boolean) =>
    action.run(() => api.updateCron(job.id, { schedule, prompt, enabled }).then(onChange)).then((ok) => ok && setEditing(false))

  async function remove() {
    if (confirmAction(`删除定时任务 ${job.name}？`)) await action.run(() => api.deleteCron(job.id).then(onChange))
  }

  return (
    <li className="card">
      <div className="card-head">
        <strong>{job.name}</strong>
        <span className="tag">{agentLabel(agents, job.agent_id)}</span>
        <code>{job.schedule}</code>
        {!job.enabled && <span className="tag subtle">已停用</span>}
      </div>
      <div className="muted small">
        下次 {formatTime(job.next_run_at)}
        {job.last_run_at && <> · 上次 {formatTime(job.last_run_at)}</>}
        {job.last_session_id && (
          <>
            {' '}· <Link to={`/sessions/${job.last_session_id}`}>上次会话</Link>
          </>
        )}
      </div>
      {job.last_error && <div className="error">触发失败已自动停用：{job.last_error}</div>}
      {editing ? (
        <div className="form">
          <Field label="cron 表达式">
            <input value={schedule} onChange={(e) => setSchedule(e.target.value)} className="mono" />
          </Field>
          <Field label="提示词">
            <textarea rows={5} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          </Field>
          <div className="row">
            <button className="button primary small" onClick={() => void update(job.enabled)} disabled={action.busy}>
              保存
            </button>
            <button className="button small" onClick={() => setEditing(false)}>
              取消
            </button>
          </div>
        </div>
      ) : (
        <p className="prewrap">{job.prompt}</p>
      )}
      <ErrorBanner error={action.error} />
      {!editing && (
        <div className="row">
          <button className="button small" onClick={() => setEditing(true)}>
            编辑
          </button>
          <button className="button small" onClick={() => void update(!job.enabled)} disabled={action.busy}>
            {job.enabled ? '停用' : '启用'}
          </button>
          <button className="button small danger" onClick={remove} disabled={action.busy}>
            删除
          </button>
        </div>
      )}
    </li>
  )
}

function CronForm({ onCreated }: { onCreated: () => void }) {
  const agents = useAgents()
  const [job, setJob] = useState({ name: '', agent_id: asAgentId(''), schedule: '*/5 * * * *', prompt: '' })
  const action = useAction()
  const set = (patch: Partial<typeof job>) => setJob((prev) => ({ ...prev, ...patch }))

  async function create() {
    if (await action.run(() => api.createCron(job))) {
      setJob({ name: '', agent_id: asAgentId(''), schedule: '*/5 * * * *', prompt: '' })
      onCreated()
    }
  }

  return (
    <details className="card">
      <summary>新建定时任务</summary>
      <div className="form">
        <Field label="名称">
          <input value={job.name} onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Field label="Agent">
          <select value={job.agent_id} onChange={(e) => set({ agent_id: asAgentId(e.target.value) })}>
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
        <Field label="cron 表达式" hint="5 段式，按服务端配置文件中的 timezone 解释">
          <input value={job.schedule} onChange={(e) => set({ schedule: e.target.value })} className="mono" />
        </Field>
        <Field label="提示词" hint="每次触发都会在新 session 中执行。定时 session 不会自动推送，需要时在提示词里要求 agent 调用 notify_user">
          <textarea rows={5} value={job.prompt} onChange={(e) => set({ prompt: e.target.value })} />
        </Field>
        <ErrorBanner error={action.error} />
        <button className="button primary" onClick={create} disabled={action.busy || !job.name || !job.agent_id || !job.prompt}>
          创建
        </button>
      </div>
    </details>
  )
}

export default function Cron() {
  const jobs = useLoad(() => api.cron(), [])
  const agents = useAgents()
  return (
    <Page title="定时任务">
      <CronForm onCreated={() => void jobs.reload()} />
      <Loading state={jobs}>
        {(list) => (
          <ul className="cards">
            {list.map((j) => (
              <CronItem key={`${j.id}-${j.schedule}-${j.enabled}-${j.prompt}`} job={j} agents={agents.data} onChange={() => void jobs.reload()} />
            ))}
            {list.length === 0 && <div className="muted center">没有定时任务</div>}
          </ul>
        )}
      </Loading>
    </Page>
  )
}
