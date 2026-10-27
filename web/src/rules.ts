// 分发规则 `kind:glob` 的前端辅助：拆分、补全候选、条目按种类与 owner 筛选。匹配本身只在服务端算（agents/permissions.py）

import type { AgentId, CatalogItem, ItemKind, ItemOf } from './api'

/** 种类的标签；键的顺序就是展示顺序，Record 保证每种都在 */
export const KIND_LABEL: Record<ItemKind, string> = { tool: '工具', skill: 'Skill', mcp: 'MCP', secret: 'Secret' }

/** 全部种类：从 KIND_LABEL 的键推导 */
export const KINDS = Object.keys(KIND_LABEL) as readonly ItemKind[]

export function isKind(value: string | null): value is ItemKind {
  return (KINDS as readonly string[]).includes(value ?? '')
}

/** `kind:name`（服务端给出的条目，kind 必然合法）拆成两半 */
export function splitRule(rule: string): { kind: ItemKind; name: string } {
  const sep = rule.indexOf(':')
  const kind = rule.slice(0, sep)
  if (!isKind(kind)) throw new Error(`not a kind:name item: ${rule}`)
  return { kind, name: rule.slice(sep + 1) }
}

/** 规则补全的候选：每种的 `kind:*` 在前，然后是全部条目 `kind:name`（按服务端给的顺序） */
export function ruleCandidates(items: CatalogItem[]): string[] {
  return [...KINDS.map((k) => `${k}:*`), ...items.map((i) => `${i.kind}:${i.name}`)]
}

/** 条目页的 owner 筛选：all 全部，platform 归平台的，其余为 agent 的 id */
export type OwnerFilter = 'all' | 'platform' | AgentId

export function filterItems<K extends ItemKind>(items: CatalogItem[], kind: K, owner: OwnerFilter): ItemOf<K>[] {
  return items.filter(
    (i): i is ItemOf<K> =>
      i.kind === kind && (owner === 'all' || (owner === 'platform' ? i.owner === null : i.owner === owner)),
  )
}
