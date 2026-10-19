import type { ReactNode } from 'react'
import { RouterProvider } from '@heroui/react'
import { useHref, useNavigate, type NavigateOptions } from 'react-router-dom'

// 让 HeroUI 组件的 href（Link、Tabs.Tab、ListBox.Item 等）走 react-router 的客户端导航
declare module 'react-aria-components' {
  interface RouterConfig {
    routerOptions: NavigateOptions
  }
}

export function HeroRouter({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  return (
    <RouterProvider navigate={(to, options) => navigate(to, options)} useHref={useHref}>
      {children}
    </RouterProvider>
  )
}
