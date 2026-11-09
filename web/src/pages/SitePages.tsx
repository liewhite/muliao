// 「页面」：agent 在工作目录 site/<名>/ 下写的网页（监控、数据展示）。列表是按 agent 分组的卡片网格、按最近更新排；点开在 iframe 里全屏显示。
// 页面在沙箱里跑（服务端 CSP sandbox，不与界面同源），iframe 再加一层同样的 sandbox；地址是接口给的签名地址。

import { useState } from 'react'
import { Button, Card } from '@heroui/react'
import { ExternalLink, RefreshCw } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { api, asAgentId, sitePageRoute, type AgentId, type SitePage } from '../api'
import { useReturnState } from '../nav'
import { groupPagesByAgent } from '../pageGroups'
import { CardGrid, EmptyHint, ErrorAlert, GroupSection, Loading, Page, agentLabel, relativeTime, useAgents, useLoad } from '../ui'

// 与服务端页面响应的 CSP sandbox（src/muliao/api/sites.py _PAGE_HEADERS）一致：浏览器取交集，改就两处一起改
const SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads'

/** 网格里的一张页面卡片，整张可点，打开页面 */
function PageCard({ page }: { page: SitePage }) {
  const from = useReturnState()
  return (
    <Link to={sitePageRoute(page.agent_id, page.name)} state={from} className="rounded-3xl outline-none focus-visible:ring-2 focus-visible:ring-accent">
      <Card className="h-full gap-1 transition-shadow hover:shadow-md">
        <div className="truncate font-medium">{page.title}</div>
        <code className="truncate font-mono text-xs text-muted">site/{page.name}</code>
        <div className="text-xs text-muted">更新于 {relativeTime(page.modified)}</div>
      </Card>
    </Link>
  )
}

/** 页面列表；给了 agent 只列它的（agent 详情页），否则按 agent 分组列全部。都按最近更新排 */
export function SitePageList({ agent }: { agent?: AgentId }) {
  const agents = useAgents()
  const pages = useLoad(() => api.pages(), [])
  return (
    <Loading state={pages}>
      {(all) => {
        const groups = groupPagesByAgent(agent ? all.filter((p) => p.agent_id === agent) : all)
        if (groups.length === 0)
          return <EmptyHint>还没有页面：agent 在工作目录写 site/&lt;页面名&gt;/index.html 即出现在这里</EmptyHint>
        const grid = (ps: SitePage[]) => (
          <CardGrid>
            {ps.map((p) => (
              <PageCard key={p.name} page={p} />
            ))}
          </CardGrid>
        )
        if (agent) return grid(groups[0].pages)
        return (
          <div className="flex flex-col gap-4">
            {groups.map((g) => (
              <GroupSection key={g.agentId} name={agentLabel(agents.data, g.agentId)} count={g.pages.length}>
                {grid(g.pages)}
              </GroupSection>
            ))}
          </div>
        )
      }}
    </Loading>
  )
}

export default function SitePages() {
  return (
    <Page title="页面" wide>
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-3">
        <SitePageList />
      </div>
    </Page>
  )
}

export function SitePageView() {
  const params = useParams()
  const agent = asAgentId(params.agent ?? '')
  const name = params.name ?? ''
  const agents = useAgents()
  const pages = useLoad(() => api.pages(), [])
  // 刷新时递增：iframe 换 key 重新加载
  const [version, setVersion] = useState(0)
  const page = pages.data?.find((p) => p.agent_id === agent && p.name === name)
  return (
    <Page
      title={page ? `${page.title} · ${agentLabel(agents.data, agent)}` : name}
      back="/pages"
      wide
      actions={
        page && (
          <>
            <Button isIconOnly variant="ghost" size="sm" aria-label="刷新" onPress={() => setVersion((v) => v + 1)}>
              <RefreshCw className="size-4" />
            </Button>
            <a href={page.url} target="_blank" rel="noreferrer" aria-label="新窗口打开" title="新窗口打开">
              <ExternalLink className="size-4" />
            </a>
          </>
        )
      }
    >
      <Loading state={pages}>
        {() =>
          page ? (
            <iframe
              key={version}
              src={page.url}
              title={page.title}
              sandbox={SANDBOX}
              referrerPolicy="no-referrer"
              className="block h-full w-full border-0 bg-white"
            />
          ) : (
            <div className="p-4">
              <ErrorAlert error={`没有这个页面：${agentLabel(agents.data, agent)} 的 site/${name}`} />
            </div>
          )
        }
      </Loading>
    </Page>
  )
}
