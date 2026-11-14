// 资产页的前端辅助：种类的标签、资产按种类与 owner 筛选。规则的语法、补全候选与匹配只在服务端（assets 的 rules.rs）

import type { AgentId, CatalogAsset, AssetKind, AssetOf } from '../api'

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

/** 资产页的 owner 筛选：all 全部，platform 归平台的，其余为 agent 的 id */
export type OwnerFilter = 'all' | 'platform' | AgentId

export function filterAssets<K extends AssetKind>(assets: CatalogAsset[], kind: K, owner: OwnerFilter): AssetOf<K>[] {
  return assets.filter(
    (i): i is AssetOf<K> =>
      i.kind === kind && (owner === 'all' || (owner === 'platform' ? i.owner === null : i.owner === owner)),
  )
}
