import type { ReactNode } from 'react'
import { useOutlet } from 'react-router-dom'

/**
 * 列表 + 详情分屏的布局路由：宽屏（xl）左列表右详情；窄屏只显示其一，打开详情时列表隐藏。
 * 详情由子路由经 Outlet 渲染，没有子路由匹配时宽屏右侧显示占位提示。
 */
export function ListDetail({ list, empty }: { list: ReactNode; empty: string }) {
  const detail = useOutlet()
  return (
    <div className="flex h-full">
      <div
        className={`h-full min-w-0 flex-col xl:flex xl:w-96 xl:flex-none xl:border-r xl:border-separator ${
          detail ? 'hidden' : 'flex flex-1'
        }`}
      >
        {list}
      </div>
      {detail ? (
        <div className="h-full min-w-0 flex-1">{detail}</div>
      ) : (
        <div className="hidden flex-1 items-center justify-center text-sm text-muted xl:flex">{empty}</div>
      )}
    </div>
  )
}
