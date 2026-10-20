import { useEffect, useState } from 'react'
import { useTheme } from '@heroui/react'
import { Bell, Bot, MessageSquare, Settings as SettingsIcon, Zap } from 'lucide-react'
import { NavLink, Navigate, Route, Routes, useNavigate, useParams } from 'react-router-dom'
import { api, unauthorizedEvent } from './api'
import { closeServerEvents, useUiOutdated } from './events'
import { useUnreadCount, useUnreadSync } from './unread'
import { AgentCreate, AgentDetail, AgentList } from './pages/Agents'
import { Login, Settings, Tasks, Vault } from './pages/Admin'
import Triggers from './pages/Triggers'
import Mcp from './pages/Mcp'
import PublicPrompt from './pages/Prompts'
import NewSession from './pages/NewSession'
import Notifications from './pages/Notifications'
import SessionView from './pages/SessionView'
import Sessions from './pages/Sessions'
import { PublicSkills, SkillEditor } from './pages/Skills'
import { syncPushSubscription } from './push'
import { ErrorAlert, errorMessage } from './ui'

type AuthState = 'checking' | 'in' | 'out'

const TABS = [
  { to: '/', label: '会话', Icon: MessageSquare },
  { to: '/notifications', label: '通知', Icon: Bell },
  { to: '/agents', label: 'Agents', Icon: Bot },
  { to: '/triggers', label: '触发', Icon: Zap },
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
  return <Shell />
}

/** 每个会话一个独立的页面实例：会话之间跳转（thread 链接、点通知）时，草稿、附件、运行中的事件与报错不带到另一个会话 */
function SessionRoute() {
  const { id = '' } = useParams()
  return <SessionView key={id} />
}

/** 登录后的界面：页面与底部导航 */
function Shell() {
  useUnreadSync()
  const unread = useUnreadCount()
  const outdated = useUiOutdated()
  return (
    <div className="flex h-full flex-col">
      <div className="min-h-0 flex-1">
        <Routes>
          <Route path="/" element={<Sessions />} />
          <Route path="/new" element={<NewSession />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/sessions/:id" element={<SessionRoute />} />
          <Route path="/agents" element={<AgentList />} />
          <Route path="/agents/new" element={<AgentCreate />} />
          <Route path="/agents/:id" element={<AgentDetail />} />
          <Route path="/triggers" element={<Triggers />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/prompt" element={<PublicPrompt />} />
          <Route path="/skills" element={<PublicSkills />} />
          <Route path="/skills/edit" element={<SkillEditor />} />
          <Route path="/mcp" element={<Mcp />} />
          <Route path="/vault" element={<Vault />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/login" element={<Navigate to="/" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
      {/* 放在底部导航上方：不占顶部安全区（页面头部已经留了），也不挡头部的按钮 */}
      {outdated && (
        <button
          type="button"
          className="bg-accent px-3 py-1.5 text-center text-sm text-accent-foreground"
          onClick={() => location.reload()}
        >
          新版本已上线，点这里刷新
        </button>
      )}
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
            <span className="relative">
              <Icon className="size-5" />
              {to === '/notifications' && unread > 0 && (
                <span className="absolute -top-1.5 left-3 min-w-4 rounded-full bg-danger px-1 text-center text-[10px] leading-4 text-danger-foreground">
                  {unread > 99 ? '99+' : unread}
                  <span className="sr-only">条未读</span>
                </span>
              )}
            </span>
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
    </div>
  )
}
