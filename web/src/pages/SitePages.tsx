// 「页面」：agent 在工作目录 site/<名>/ 下写的网页（监控、数据展示）。列表按 agent 分组；点开在 iframe 里全屏显示。
// 页面在沙箱里跑（服务端 CSP sandbox，不与界面同源），iframe 再加一层同样的 sandbox；地址是接口给的签名地址。

import { useState } from 'react'
import { Button, Card } from '@heroui/react'
import { ChevronRight, ExternalLink, RefreshCw } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { api, asAgentId, sitePageRoute, type AgentId, type SitePage } from '../api'
import { useReturnState } from '../nav'
import { EmptyHint, ErrorAlert, Loading, Page, agentLabel, relativeTime, useAgents, useLoad } from '../ui'

const SANDBOX = 'allow-scripts allow-forms allow-popups allow-modals allow-downloads'

/** 页面列表；给了 agent 只列它的（agent 详情页），否则按 agent 分组列全部 */
export function SitePageList({ agent }: { agent?: AgentId }) {
  const from = useReturnState()
  const agents = useAgents()
  const pages = useLoad(() => api.pages(), [])
  return (
    <Loading state={pages}>
      {(all) => {
        const list = agent ? all.filter((p) => p.agent_id === agent) : all
        if (list.length === 0)
          return <EmptyHint>还没有页面：agent 在工作目录写 site/&lt;页面名&gt;/index.html 即出现在这里</EmptyHint>
        const groups = new Map<AgentId, SitePage[]>()
        for (const p of list) groups.set(p.agent_id, [...(groups.get(p.agent_id) ?? []), p])
        return (
          <div className="flex flex-col gap-4">
            {[...groups].map(([id, ps]) => (
              <section key={id} className="flex flex-col gap-2">
                {!agent && <h2 className="text-sm font-medium text-muted">{agentLabel(agents.data, id)}</h2>}
                {ps.map((p) => (
                  <Link key={p.name} to={sitePageRoute(p.agent_id, p.name)} state={from}>
                    <Card className="transition-colors hover:bg-surface-hover">
                      <Card.Content className="flex flex-row items-center gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="truncate font-medium">{p.title}</div>
                          <div className="text-xs text-muted">
                            site/{p.name} · 更新于 {relativeTime(p.modified)}
                          </div>
                        </div>
                        <ChevronRight className="size-4 shrink-0 text-muted" aria-hidden />
                      </Card.Content>
                    </Card>
                  </Link>
                ))}
              </section>
            ))}
          </div>
        )
      }}
    </Loading>
  )
}

export default function SitePages() {
  return (
    <Page title="页面">
      <SitePageList />
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
