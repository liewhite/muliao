// 归属的前端视角：资源（MCP server、secret）归属某个 agent（私有）或公共（owner 为 null）。
// 列表接口按 owner 视角返回它私有的与公共的资源，公共项是否被同名私有项遮蔽由后端给出（shadowed），
// 遮蔽规则只在后端（agents/ownership.py）实现；这里只按归属把列表分成两段。

import type { AgentId } from './api'

/** 一条有归属的资源：owner 为 null 即公共，否则属于该 agent。 */
export interface Owned {
  owner: AgentId | null
}

/** owner 视角的列表分成两段：own 是 owner 自己管理的；inherited 是私有视角里继承来的公共资源（公共视角下为空）。 */
export function splitView<T extends Owned>(view: T[], owner: AgentId | null): { own: T[]; inherited: T[] } {
  return {
    own: view.filter((x) => x.owner === owner),
    inherited: owner === null ? [] : view.filter((x) => x.owner === null),
  }
}
