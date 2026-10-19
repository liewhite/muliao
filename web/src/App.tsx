import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, unauthorizedEvent } from './api'
import { closeServerEvents } from './events'
import { AgentCreate, AgentDetail, AgentList } from './pages/Agents'
import { Login, Settings, Tasks, Vault, Webhooks } from './pages/Admin'
import Cron from './pages/Cron'
import Mcp from './pages/Mcp'
import NewSession from './pages/NewSession'
import SessionView from './pages/SessionView'
import Sessions from './pages/Sessions'
import { PublicSkills, SkillEditor } from './pages/Skills'
import { syncPushSubscription } from './push'
import { errorMessage } from './ui'

type AuthState = 'checking' | 'in' | 'out'

const TABS = [
  ['/', '会话', '💬'],
  ['/agents', 'Agents', '🤖'],
  ['/cron', '定时', '⏰'],
  ['/settings', '设置', '⚙️'],
] as const

export default function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  const [bootError, setBootError] = useState<string | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    api.me().then(
      () => setAuth('in'),
      (e) => {
        if (e?.status === 401) setAuth('out')
        else setBootError(errorMessage(e))
      },
    )
    const onUnauthorized = () => {
      closeServerEvents()
      setAuth('out')
    }
    unauthorizedEvent.addEventListener('unauthorized', onUnauthorized)
    return () => unauthorizedEvent.removeEventListener('unauthorized', onUnauthorized)
  }, [])

  useEffect(() => {
    if (auth !== 'in') return
    void syncPushSubscription().catch((e) => console.error('push subscription sync failed', e))
    // 通知点击时 Service Worker 让已打开的窗口跳转
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'navigate') navigate(e.data.url)
    }
    navigator.serviceWorker?.addEventListener('message', onMessage)
    return () => navigator.serviceWorker?.removeEventListener('message', onMessage)
  }, [auth, navigate])

  if (bootError) return <div className="error center">无法连接服务端：{bootError}</div>
  if (auth === 'checking') return null
  if (auth === 'out') {
    return (
      <Routes>
        <Route path="*" element={<Login onLogin={() => setAuth('in')} />} />
      </Routes>
    )
  }

  return (
    <div className="app">
      <div className="content">
        <Routes>
          <Route path="/" element={<Sessions />} />
          <Route path="/new" element={<NewSession />} />
          <Route path="/sessions/:id" element={<SessionView />} />
          <Route path="/agents" element={<AgentList />} />
          <Route path="/agents/new" element={<AgentCreate />} />
          <Route path="/agents/:id" element={<AgentDetail />} />
          <Route path="/cron" element={<Cron />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/skills" element={<PublicSkills />} />
          <Route path="/skills/edit" element={<SkillEditor />} />
          <Route path="/mcp" element={<Mcp />} />
          <Route path="/vault" element={<Vault />} />
          <Route path="/webhooks" element={<Webhooks />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      <nav className="tabbar">
        {TABS.map(([to, label, icon]) => (
          <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => (isActive ? 'active' : '')}>
            <span className="tab-icon">{icon}</span>
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
