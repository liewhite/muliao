import { createContext, useContext } from 'react'
import {
  Activity,
  BarChart3,
  Bell,
  Bot,
  KanbanSquare,
  KeyRound,
  MessageSquare,
  Plug,
  ScrollText,
  Settings,
  Sparkles,
  Zap,
  type LucideIcon,
} from 'lucide-react'

export type NavItem = { to: string; label: string; Icon: LucideIcon }

/** 首页导航：随时在变的动态（手机底栏、桌面左侧栏） */
export const ACTIVITY_TABS: readonly NavItem[] = [
  { to: '/', label: '会话', Icon: MessageSquare },
  { to: '/issues', label: 'Issues', Icon: KanbanSquare },
  { to: '/notifications', label: '通知', Icon: Bell },
]

/** 管理菜单：配置类页面，从左上角按钮打开 */
export const ADMIN_GROUPS: readonly { title: string; items: readonly NavItem[] }[] = [
  {
    title: '运行',
    items: [
      { to: '/agents', label: 'Agents', Icon: Bot },
      { to: '/triggers', label: '触发', Icon: Zap },
      { to: '/tasks', label: '任务与失败记录', Icon: Activity },
      { to: '/usage', label: 'Token 用量', Icon: BarChart3 },
    ],
  },
  {
    title: '公共资源',
    items: [
      { to: '/memory', label: '全局记忆', Icon: ScrollText },
      { to: '/skills', label: '公共 Skills', Icon: Sparkles },
      { to: '/mcp', label: '公共 MCP servers', Icon: Plug },
      { to: '/vault', label: '公共 Vault', Icon: KeyRound },
    ],
  },
  {
    title: '本设备',
    items: [{ to: '/settings', label: '推送与登录', Icon: Settings }],
  },
]

/** 路径是否属于管理菜单里的某一项（含其子页面，如 /agents/:id） */
export function isAdminPath(pathname: string): boolean {
  return ADMIN_GROUPS.some((g) => g.items.some((i) => pathname === i.to || pathname.startsWith(`${i.to}/`)))
}

/** 打开管理菜单（抽屉）；由登录后的外壳提供 */
export const AdminMenuContext = createContext<() => void>(() => {
  throw new Error('AdminMenuContext 未提供：页面必须渲染在 Shell 里')
})

export function useOpenAdminMenu(): () => void {
  return useContext(AdminMenuContext)
}
