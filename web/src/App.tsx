import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, useTheme } from '@heroui/react'
import { Menu } from 'lucide-react'
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom'
import { api, unauthorizedEvent } from './api'
import { closeServerEvents, useUiOutdated } from './events'
import { useUnreadCount, useUnreadSync } from './unread'
import { AgentCreate, AgentDetail, AgentList } from './pages/Agents'
import { Login, Settings, Tasks } from './pages/Admin'
import { Roles } from './pages/Permissions'
import Triggers from './pages/Triggers'
import Issues from './pages/Issues'
import IssueView from './pages/IssueView'
import Mcp from './pages/Mcp'
import Vault from './pages/Vault'
import GlobalMemory from './pages/Memory'
import NewSession from './pages/NewSession'
import Notifications from './pages/Notifications'
import SessionView from './pages/SessionView'
import Sessions from './pages/Sessions'
import { PublicSkills, SkillEditor } from './pages/Skills'
import UsagePage from './pages/Usage'
import { syncPushSubscription } from './push'
import { handleNavigation } from './swNavigate'
import { ErrorAlert, errorMessage } from './ui'
import { AdminDrawer, AdminNav } from './AdminMenu'
import { ListDetail } from './ListDetail'
import { ACTIVITY_TABS, AdminMenuContext, isAdminPath } from './nav'

type AuthState = 'checking' | 'in' | 'out'

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

  // BrowserRouter 下 navigate 随路径变化换新函数：放进 ref，下面的 effect 只随登录状态执行，不在每次切页时重跑（#135）
  const navigateRef = useRef(navigate)
  useEffect(() => {
    navigateRef.current = navigate
  }, [navigate])

  useEffect(() => {
    if (auth !== 'in') return
    // 订阅可能被浏览器轮换：登录后（页面可见时）和每次回到前台时同步一次
    const sync = () => {
      if (document.visibilityState !== 'visible') return
      syncPushSubscription().catch((e) => console.error('push subscription sync failed', e))
    }
    sync()
    document.addEventListener('visibilitychange', sync)
    // 点通知后跳到 swNavigate 取到的页面（含外壳挂载前就取到的那次，见 swNavigate.ts）
    const stop = handleNavigation((url) => navigateRef.current(url))
    return () => {
      document.removeEventListener('visibilitychange', sync)
      stop()
    }
  }, [auth])

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

/** 每个 agent 一个独立的页面实例：分屏切换 agent 时，标签页与表单草稿不带到另一个 agent */
function AgentRoute() {
  const { id = '' } = useParams()
  return <AgentDetail key={id} />
}

/** 每个 issue 一个独立的页面实例：编辑中的草稿不带到另一个 issue */
function IssueRoute() {
  const { id = '' } = useParams()
  return <IssueView key={id} />
}

/** 动态导航的一项，通知带未读角标；底栏与桌面左侧栏共用 */
function TabLink({ to, label, Icon, unread, className }: (typeof ACTIVITY_TABS)[number] & { unread: number; className: string }) {
  return (
    <NavLink
      to={to}
      end={to === '/'}
      className={({ isActive }) =>
        `flex flex-col items-center justify-center gap-0.5 text-[11px] ${className} ${isActive ? 'text-accent' : 'text-muted'}`
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
  )
}

/**
 * 登录后的界面。首页导航只放动态（会话、Issues、通知），配置类页面都收进管理菜单。
 * 手机：页面 + 底部动态导航；页面头部左上角的按钮打开管理抽屉。
 * 桌面（md 起）：左侧窄栏（左上角管理菜单按钮 + 动态导航）+ 页面；管理类页面在 lg 起多一列常驻的管理菜单。
 * 会话、Agents 在 xl 起列表与详情分屏（见 ListDetail）。
 */
function Shell() {
  useUnreadSync()
  const unread = useUnreadCount()
  const outdated = useUiOutdated()
  const { pathname } = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)
  const openMenu = useCallback(() => setMenuOpen(true), [])
  return (
    <AdminMenuContext value={openMenu}>
      <div className="flex h-full flex-col md:flex-row">
        <nav
          aria-label="动态"
          className="hidden w-16 shrink-0 flex-col items-center gap-1 border-r border-separator bg-surface pt-[calc(env(safe-area-inset-top)+0.5rem)] pb-2 md:flex"
        >
          <Button isIconOnly variant="ghost" aria-label="管理菜单" onPress={openMenu}>
            <Menu className="size-5" />
          </Button>
          <div className="my-1 h-px w-8 bg-separator" />
          {ACTIVITY_TABS.map((t) => (
            <TabLink key={t.to} {...t} unread={unread} className="h-14 w-14 rounded-lg hover:bg-surface-hover" />
          ))}
        </nav>
        {isAdminPath(pathname) && (
          <aside className="hidden w-56 shrink-0 flex-col border-r border-separator bg-surface lg:flex">
            <div className="border-b border-separator px-6 pt-[calc(env(safe-area-inset-top)+0.5rem)] pb-2 text-lg leading-8 font-semibold">
              管理
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto">
              <AdminNav />
            </div>
          </aside>
        )}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1">
            <Routes>
              <Route path="/" element={<ListDetail list={<Sessions />} empty="选择一个会话" />}>
                <Route path="new" element={<NewSession />} />
                <Route path="sessions/:id" element={<SessionRoute />} />
              </Route>
              <Route path="/notifications" element={<Notifications />} />
              <Route path="/agents" element={<ListDetail list={<AgentList />} empty="选择一个 agent" />}>
                <Route path="new" element={<AgentCreate />} />
                <Route path=":id" element={<AgentRoute />} />
              </Route>
              <Route path="/roles" element={<Roles />} />
              <Route path="/triggers" element={<Triggers />} />
              <Route path="/issues" element={<Issues />} />
              <Route path="/issues/:id" element={<IssueRoute />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/memory" element={<GlobalMemory />} />
              <Route path="/skills" element={<PublicSkills />} />
              <Route path="/skills/edit" element={<SkillEditor />} />
              <Route path="/mcp" element={<Mcp />} />
              <Route path="/vault" element={<Vault />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/usage" element={<UsagePage />} />
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
          <nav
            aria-label="动态"
            className="flex border-t border-separator bg-surface pb-[env(safe-area-inset-bottom)] md:hidden"
          >
            {ACTIVITY_TABS.map((t) => (
              <TabLink key={t.to} {...t} unread={unread} className="h-14 flex-1" />
            ))}
          </nav>
        </div>
      </div>
      <AdminDrawer isOpen={menuOpen} onOpenChange={setMenuOpen} />
    </AdminMenuContext>
  )
}
