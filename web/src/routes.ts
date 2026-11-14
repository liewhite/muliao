// 前端路由的地址：只在这里拼。页面路由表在 App.tsx。sessionRoute、issueRoute 与后端各写一份（通知点开的地址），
// server/crates/muliao/tests/frontend_contract.rs 核对

import type { AgentId, IssueId, SessionId } from './api'

/** agent 页按 id（名称可改） */
export const agentRoute = (id: AgentId) => `/agents/${id}`
/** 会话页；通知点开的地址由后端 notifications 的 inbox::url_of 生成，与它一致 */
export const sessionRoute = (id: SessionId) => `/sessions/${id}`
export const newSessionRoute = (agentId: AgentId) => `/new?${new URLSearchParams({ agent: agentId })}`
/** issue 页；与后端 muliao_issues::route 一致（交给用户审核的通知点开它）。Service Worker（public/sw.js）按这个形状认出待审核 issue 的通知 */
export const issueRoute = (id: IssueId) => `/issues/${id}`
/** agent 自定义页面的界面路由（页面本身在沙箱 iframe 里按接口给的签名地址打开） */
export const sitePageRoute = (agentId: AgentId, name: string) =>
  `/pages/${encodeURIComponent(agentId)}/${encodeURIComponent(name)}`
