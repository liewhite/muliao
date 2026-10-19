import { useEffect, useState } from 'react'
import { useTheme } from '@heroui/react'
import { Bot, Clock, MessageSquare, Settings as SettingsIcon } from 'lucide-react'
import { NavLink, Navigate, Route, Routes, useNavigate } from 'react-router-dom'
import { api, unauthorizedEvent } from './api'
import { closeServerEvents } from './events'
import { AgentCreate, AgentDetail, AgentList } from './pages/Agents'
import { Login, Settings, Tasks, Vault } from './pages/Admin'
import Cron from './pages/Cron'
import Mcp from './pages/Mcp'
import NewSession from './pages/NewSession'
import SessionView from './pages/SessionView'
import Sessions from './pages/Sessions'
import { PublicSkills, SkillEditor } from './pages/Skills'
import { syncPushSubscription } from './push'
import { ErrorAlert, errorMessage } from './ui'

type AuthState = 'checking' | 'in' | 'out'

const TABS = [
  { to: '/', label: '会话', Icon: MessageSquare },
  { to: '/agents', label: 'Agents', Icon: Bot },
  { to: '/cron', label: '定时', Icon: Clock },
  { to: '/settings', label: '设置', Icon: SettingsIcon },
] as const

export default function App() {
  const [auth, setAuth] = useState<AuthState>('checking')
  const [bootError, setBootError] = useState<string | null>(null)
  const navigate = useNavigate()
  // 主题跟随系统深浅色（index.html 里的内联脚本负责首屏，避免闪烁）
  useTheme('system')

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

  if (bootError) {
    return (
      <div className="p-4">
        <ErrorAlert title="无法连接服务端" error={bootError} />
      </div>
    )
  }
  if (auth === 'checking') return null
  if (auth === 'out') {
    return (
      <Routes>
        <Route path="*" element={<Login onLogin={() => setAuth('in')} />} />
      </Routes>
    )
  }

  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1">
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
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      <nav className="flex border-t border-separator bg-surface pb-[env(safe-area-inset-bottom)]">
        {TABS.map(({ to, label, Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex h-14 flex-1 flex-col items-center justify-center gap-0.5 text-[11px] ${isActive ? 'text-accent' : 'text-muted'}`
            }
          >
            <Icon className="size-5" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
