// 分发规则 `action:kind:glob` 的前端辅助：拆分、补全候选、资产按种类与 owner 筛选。匹配本身只在服务端算（assets 的 rules.rs）

import type { AgentId, CatalogAsset, AssetKind, AssetOf } from './api'

/** 种类的标签；键的顺序就是展示顺序，Record 保证每种都在 */
export const KIND_LABEL: Record<AssetKind, string> = {
  tool: '工具',
  skill: 'Skill',
  mcp: 'MCP',
  secret: 'Secret',
  memory: '记忆',
}

/** 全部种类：从 KIND_LABEL 的键推导 */
export const KINDS = Object.keys(KIND_LABEL) as readonly AssetKind[]

export function isKind(value: string | null): value is AssetKind {
  return (KINDS as readonly string[]).includes(value ?? '')
}

/** `kind:name`（服务端给出的能用的资产，kind 必然合法）拆成两半 */
export function splitRule(rule: string): { kind: AssetKind; name: string } {
  const sep = rule.indexOf(':')
  const kind = rule.slice(0, sep)
  if (!isKind(kind)) throw new Error(`not a kind:name asset: ${rule}`)
  return { kind, name: rule.slice(sep + 1) }
}

/** 规则补全的候选：每种的 `use:kind:*` 在前，然后是每项资产的 `use:kind:name`（按服务端给的顺序）；存在库里的资产还有
 * `*:kind:name`（能用也能改；工具只有 use） */
export function ruleCandidates(assets: CatalogAsset[]): string[] {
  return [
    ...KINDS.map((k) => `use:${k}:*`),
    ...assets.flatMap((i) => [`use:${i.kind}:${i.name}`, ...(i.kind === 'tool' ? [] : [`*:${i.kind}:${i.name}`])]),
  ]
}

/** 资产页的 owner 筛选：all 全部，platform 归平台的，其余为 agent 的 id */
export type OwnerFilter = 'all' | 'platform' | AgentId

export function filterAssets<K extends AssetKind>(assets: CatalogAsset[], kind: K, owner: OwnerFilter): AssetOf<K>[] {
  return assets.filter(
    (i): i is AssetOf<K> =>
      i.kind === kind && (owner === 'all' || (owner === 'platform' ? i.owner === null : i.owner === owner)),
  )
}
