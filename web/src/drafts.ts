// 编辑草稿与服务端的新值：编辑期间 agent、自进化、别的页面都可能改同一份数据（页面随 changed 事件重新拉取）。
// 没动过的草稿跟上服务端；动过的保持不动，保存时只提交改过的字段（#575），同一字段两边都改了时后保存的覆盖先保存的
// （#646 删了按字段乐观锁）。整份保存、带版本的文档（记忆、skill）把版本放在草稿里一起冻结，交回打开编辑时的版本。

import { useState } from 'react'

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

const keysOf = <T extends object>(t: T) => Object.keys(t) as (keyof T)[]

/** 改过的字段：草稿与基线不同 */
export function editedFields<T extends object>(base: T, draft: T): (keyof T)[] {
  return keysOf(draft).filter((k) => !same(draft[k], base[k]))
}

/** 草稿的状态：基线（开始编辑时服务端的值）与草稿，以及每个字段的草稿被外部（不是用户输入）换掉的次数 */
export type DraftState<T extends object> = { base: T; draft: T; replaced: Partial<Record<keyof T, number>> }

export function draftState<T extends object>(latest: T): DraftState<T> {
  return { base: latest, draft: latest, replaced: {} }
}

/** 基线与草稿都换成 next（服务端的新值或保存的结果）；值变了的字段 replaced 记一次 */
export function replacedBy<T extends object>(s: DraftState<T>, next: T): DraftState<T> {
  const replaced = { ...s.replaced }
  for (const k of keysOf(next)) if (!same(s.draft[k], next[k])) replaced[k] = (replaced[k] ?? 0) + 1
  return { base: next, draft: next, replaced }
}

/** 服务端有了新值：还没改过的草稿跟上它，改过的不动 */
export function followed<T extends object>(s: DraftState<T>, latest: T): DraftState<T> {
  return editedFields(s.base, s.draft).length === 0 ? replacedBy(s, latest) : s
}

/**
 * 编辑表单的草稿：latest 是服务端此刻的值。
 * - 没改过时跟上 latest；改过之后草稿和基线都不再跟（未改的字段不提交，不会盖掉别人的改动）。
 * - 保存提交 changes（改过的字段）；保存成功后 saved(结果)：以服务端存下的值（可能规整过）为新的起点。
 */
export function useDraft<T extends object>(latest: T) {
  const [seen, setSeen] = useState(latest)
  const [state, setState] = useState(() => draftState(latest))
  if (!same(latest, seen)) {
    setSeen(latest)
    setState(followed(state, latest))
  }
  const { base, draft } = state
  const edited = editedFields(base, draft)
  return {
    draft,
    set: (patch: Partial<T>) => setState((prev) => ({ ...prev, draft: { ...prev.draft, ...patch } })),
    // 改过的字段；要换算后再提交的（如把文字解析成编号）自己从 changes 与 base 拼请求
    changes: Object.fromEntries(edited.map((k) => [k, draft[k]])) as Partial<T>,
    // 开始编辑时服务端的值（全部字段）
    base,
    // 只在挂载时读初值的编辑器拿它做 key：草稿被外部换掉（跟上服务端、保存后）就重新挂载，用户输入不会
    replaced: (k: keyof T) => state.replaced[k] ?? 0,
    saved: (result: T) => setState((prev) => replacedBy(prev, result)),
    // 放弃修改，换成服务端此刻的值（整份保存因版本对不上被拒之后，由它重新开始）
    discard: () => setState((prev) => replacedBy(prev, latest)),
  }
}
