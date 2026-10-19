import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, type Origin } from '../api'
import { useServerEvents } from '../events'
import { Loading, Page, relativeTime, useLoad } from '../ui'

const ORIGIN_LABEL: Record<Origin, string> = {
  user: '对话',
  cron: '定时',
  webhook: 'Webhook',
  telegram: 'Telegram',
  delegation: '委派',
}

// 定时任务每次触发都会产生 session，默认不显示以免淹没收件箱
const FILTERS: { label: string; origins: Origin[] | undefined }[] = [
  { label: '收件箱', origins: ['user', 'delegation', 'webhook', 'telegram'] },
  { label: '定时', origins: ['cron'] },
  { label: '全部', origins: undefined },
]

export default function Sessions() {
  const [filter, setFilter] = useState(0)
  const [agent, setAgent] = useState('')
  const agents = useLoad(() => api.agents(), [])
  const sessions = useLoad(
    () => api.sessions({ origin: FILTERS[filter].origins, agent: agent || undefined, limit: 100 }),
    [filter, agent],
  )
  useServerEvents((e) => {
    if (e.type === 'reconnected' || (e.type === 'task' && e.kind === 'turn')) void sessions.reload()
  })

  return (
    <Page
      title="会话"
      actions={
        <Link className="button primary small" to="/new">
          新对话
        </Link>
      }
    >
      <div className="toolbar">
        <div className="segmented">
          {FILTERS.map((f, i) => (
            <button key={f.label} className={i === filter ? 'active' : ''} onClick={() => setFilter(i)}>
              {f.label}
            </button>
          ))}
        </div>
        <select value={agent} onChange={(e) => setAgent(e.target.value)}>
          <option value="">所有 agent</option>
          {agents.data?.map((a) => (
            <option key={a.name} value={a.name}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
      <Loading state={sessions}>
        {(list) =>
          list.length === 0 ? (
            <div className="muted center">没有会话</div>
          ) : (
            <ul className="list">
              {list.map((s) => (
                <li key={s.id}>
                  <Link to={`/sessions/${s.id}`} className="list-item">
                    <div className="list-main">
                      <div className="list-title">
                        {s.unread && <span className="dot" aria-label="未读" />}
                        {s.title}
                      </div>
                      <div className="list-sub">
                        <span className="tag">{s.agent_name}</span>
                        <span className="tag subtle">{ORIGIN_LABEL[s.origin]}</span>
                      </div>
                    </div>
                    <div className="list-meta">{relativeTime(s.updated_at)}</div>
                  </Link>
                </li>
              ))}
            </ul>
          )
        }
      </Loading>
    </Page>
  )
}
