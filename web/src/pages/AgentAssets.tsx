import type { ReactNode } from 'react'
import { Chip, Disclosure } from '@heroui/react'
import { Link } from 'react-router-dom'
import { api, type AgentAsset, type AgentId } from '../api'
import { GroupSection, Loading, useLoad } from '../ui'

/** 来源：它名下的、放行它的角色（含 base）、它自己的 allow 规则 */
function SourceChips({ source }: { source: AgentAsset['source'] }) {
  return (
    <>
      {source.own && (
        <Chip size="sm" variant="soft" color="accent">
          自有
        </Chip>
      )}
      {source.roles.map((r) => (
        <Chip key={r} size="sm" variant="secondary">
          角色 {r}
        </Chip>
      ))}
      {source.allow && (
        <Chip size="sm" variant="secondary">
          allow 规则
        </Chip>
      )}
    </>
  )
}

function AssetRow({ asset, extra }: { asset: AgentAsset; extra?: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 border-b border-separator py-2 last:border-b-0">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="me-1 font-mono text-sm font-medium">{asset.name}</span>
        {extra}
        <SourceChips source={asset.source} />
      </div>
      {asset.description && <div className="text-xs text-muted">{asset.description}</div>}
    </div>
  )
}

function Rows<T extends AgentAsset>({
  assets,
  empty,
  extra,
}: {
  assets: T[]
  empty: string
  extra?: (a: T) => ReactNode
}) {
  if (assets.length === 0) return <p className="py-2 text-sm text-muted">{empty}</p>
  return (
    <div className="flex flex-col">
      {assets.map((a) => (
        <AssetRow key={a.name} asset={a} extra={extra?.(a)} />
      ))}
    </div>
  )
}

/** agent 能用的资产：记忆（含合并后注入指令的预览）、skill、secret、MCP，各标来源；服务端按与运行时同一套规则算 */
export function AgentAssetsView({ agent }: { agent: AgentId }) {
  const assets = useLoad(() => api.agentAssets(agent), [agent])
  return (
    <Loading state={assets}>
      {(a) => (
        <div className="flex flex-col gap-3">
          <p className="text-sm text-muted">
            它运行时能用的资产与来源（自有＝它名下的；角色＝分发规则放行；allow＝它自己的规则）。改分发在「配置」，改内容在{' '}
            <Link to="/items" className="text-accent">
              AI 资产
            </Link>
            。
          </p>
          <GroupSection name="记忆" count={a.memories.length}>
            <Rows
              assets={a.memories}
              empty="没有记忆"
              extra={(m) => (
                <>
                  <Chip size="sm" variant="soft" color={m.editable ? 'warning' : 'default'}>
                    {m.editable ? '可改' : '只读'}
                  </Chip>
                  <Chip size="sm" variant="soft">
                    {m.source.own ? '私有' : '共享'}
                  </Chip>
                </>
              )}
            />
            <Disclosure>
              <Disclosure.Heading>
                <Disclosure.Trigger className="flex items-center gap-1 pt-2 text-sm text-muted">
                  <Disclosure.Indicator className="ms-0" />
                  合并后注入指令的样子
                </Disclosure.Trigger>
              </Disclosure.Heading>
              <Disclosure.Content>
                <Disclosure.Body className="pt-2">
                  <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap rounded bg-surface-secondary p-3 font-mono text-xs">
                    {a.memory_preview}
                  </pre>
                </Disclosure.Body>
              </Disclosure.Content>
            </Disclosure>
          </GroupSection>
          <GroupSection name="skill" count={a.skills.length}>
            <Rows assets={a.skills} empty="没有 skill" />
          </GroupSection>
          <GroupSection name="secret（vault）" count={a.secrets.length}>
            <Rows assets={a.secrets} empty="没有 secret" />
          </GroupSection>
          <GroupSection name="MCP" count={a.mcp.length}>
            <Rows
              assets={a.mcp}
              empty="没有 MCP server"
              extra={(s) => (
                <>
                  <Chip size="sm" variant="soft">
                    {s.transport}
                  </Chip>
                  <Chip size="sm" variant="soft" color={s.enabled ? 'success' : 'default'}>
                    {s.enabled ? '启用' : '停用'}
                  </Chip>
                </>
              )}
            />
          </GroupSection>
        </div>
      )}
    </Loading>
  )
}
