// 归属规则的前端视角（后端在 ownership.visible 里实现同一套规则）：资源归属某个 agent（私有）
// 或公共（owner 为 null）；agent 看到自己的私有资源加上公共资源，同名时私有遮蔽公共。
// skill、MCP server、secret 共用这套规则，这里只写一遍。

import type { AgentId } from './api'

/** 一条有归属的资源：owner 为 null 即公共，否则属于该 agent。 */
export interface Owned {
  owner: AgentId | null
}

/** all 里归属 owner 的资源；owner 为 null 时即公共资源。 */
export function ownedBy<T extends Owned>(all: T[], owner: AgentId | null): T[] {
  return all.filter((x) => x.owner === owner)
}

/** 一条继承来的公共资源；shadowed 表示 owner 有同名私有项，实际生效的是私有的那条。 */
export interface Inherited<T> {
  item: T
  shadowed: boolean
}

/**
 * owner 继承到的公共资源，按 keyOf 给出的名称判断是否被同名私有项遮蔽
 * （MCP server 是 name，secret 是 key）。owner 为 null 是公共视角本身，不继承任何东西。
 */
export function inheritedPublic<T extends Owned>(
  all: T[],
  owner: AgentId | null,
  keyOf: (x: T) => string,
): Inherited<T>[] {
  if (owner === null) return []
  const own = new Set(ownedBy(all, owner).map(keyOf))
  return ownedBy(all, null).map((item) => ({ item, shadowed: own.has(keyOf(item)) }))
}
