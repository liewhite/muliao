import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { ErrorBanner, Field, Loading, Page, useAction, useLoad } from '../ui'

export default function NewSession() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const agents = useLoad(() => api.agents(), [])
  const [agent, setAgent] = useState(params.get('agent') ?? '')
  const [prompt, setPrompt] = useState('')
  const action = useAction()

  async function start() {
    await action.run(async () => {
      const session = await api.startSession(agent, prompt.trim())
      navigate(`/sessions/${session.id}`, { replace: true })
    })
  }

  return (
    <Page title="新对话" back="/">
      <Loading state={agents}>
        {(list) =>
          list.length === 0 ? (
            <div className="empty">
              <p>还没有 agent。</p>
              <Link className="button primary" to="/agents/new">
                创建 agent
              </Link>
            </div>
          ) : (
            <div className="form">
              <Field label="Agent">
                <select value={agent} onChange={(e) => setAgent(e.target.value)}>
                  <option value="" disabled>
                    选择 agent
                  </option>
                  {list.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.name} — {a.description}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="消息">
                <textarea rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)} autoFocus />
              </Field>
              <ErrorBanner error={action.error} />
              <button className="button primary" disabled={!agent || !prompt.trim() || action.busy} onClick={start}>
                开始
              </button>
            </div>
          )
        }
      </Loading>
    </Page>
  )
}
