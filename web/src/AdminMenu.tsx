import { Drawer } from '@heroui/react'
import { NavLink } from 'react-router-dom'
import { ADMIN_GROUPS } from './nav'

/** 管理菜单的分组列表；抽屉和桌面端管理页的左栏共用 */
export function AdminNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="管理" className="flex flex-col gap-4 p-3">
      {ADMIN_GROUPS.map((g) => (
        <div key={g.title} className="flex flex-col gap-0.5">
          <div className="px-3 pb-1 text-xs font-medium text-muted">{g.title}</div>
          {g.items.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={onNavigate}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive ? 'bg-accent/10 font-medium text-accent' : 'hover:bg-surface-hover'
                }`
              }
            >
              <Icon className="size-4 shrink-0" />
              <span className="truncate">{label}</span>
            </NavLink>
          ))}
        </div>
      ))}
    </nav>
  )
}

/** 从左侧滑出的管理菜单，点任一项后收起 */
export function AdminDrawer({ isOpen, onOpenChange }: { isOpen: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Drawer isOpen={isOpen} onOpenChange={onOpenChange}>
      <Drawer.Backdrop>
        <Drawer.Content placement="left" className="w-72 max-w-[85vw]">
          <Drawer.Dialog aria-label="管理菜单" className="pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]">
            <Drawer.Header>
              <Drawer.Heading>管理</Drawer.Heading>
            </Drawer.Header>
            <Drawer.Body className="p-0">
              <AdminNav onNavigate={() => onOpenChange(false)} />
            </Drawer.Body>
          </Drawer.Dialog>
        </Drawer.Content>
      </Drawer.Backdrop>
    </Drawer>
  )
}
