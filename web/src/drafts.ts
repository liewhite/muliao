// 编辑草稿与服务端的新值：编辑期间 agent、自进化、别的页面都可能改同一份数据（页面随 changed 事件重新拉取），
// 草稿既不能被新值冲掉，保存时也不能用打开时的旧值盖掉别人的改动。

import { useState } from 'react'

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/** draft 里相对 base 改过的字段（只提交这些：没动的字段别人可能改过） */
export function changedFields<T extends object>(base: T, draft: T): Partial<T> {
  return Object.fromEntries(Object.entries(draft).filter(([k, v]) => !same(v, base[k as keyof T]))) as Partial<T>
}

/** 草稿跟上服务端的新值：用户没动过的字段（与打开时相同）换成新值，动过的保留 */
export function rebased<T extends object>(opened: T, draft: T, latest: T): T {
  const next = { ...draft }
  for (const k of Object.keys(latest) as (keyof T)[]) {
    if (same(draft[k], opened[k])) next[k] = latest[k]
  }
  return next
}

/**
 * 按字段保存的表单（只提交改过的字段）：latest 是服务端的当前值，变了时没动过的字段跟上它。
 * 返回草稿、改草稿、相对当前值改过的字段，以及保存成功后以保存结果为新起点。
 */
export function useFieldDraft<T extends object>(latest: T) {
  const [opened, setOpened] = useState(latest)
  const [draft, setDraft] = useState(latest)
  if (!same(latest, opened)) {
    setOpened(latest)
    setDraft(rebased(opened, draft, latest))
  }
  return {
    draft,
    set: (patch: Partial<T>) => setDraft((prev) => ({ ...prev, ...patch })),
    changes: changedFields(opened, draft),
  }
}

/**
 * 整份保存、带版本的文档（记忆、skill）的草稿：保存时交回打开时的版本，期间被别处改过服务端 409。
 * 没改过时跟上服务端的新版本；改过了就留在打开时的版本上（stale），由用户决定丢掉自己的修改载入最新（discard）。
 */
export function useVersionedDraft<T extends { version: string }, D>(latest: T, toDraft: (t: T) => D) {
  const [base, setBase] = useState(latest)
  const [draft, setDraft] = useState(() => toDraft(latest))
  const dirty = !same(draft, toDraft(base))
  // 服务端有了新版本：没改过就跟上；草稿正好就是新版本的内容（多半是自己刚保存的）也跟上。只有 latest 一个来源，
  // 保存后不另设起点，晚到的旧结果也不会把它换回去
  if (latest.version !== base.version && (!dirty || same(draft, toDraft(latest)))) {
    setBase(latest)
    setDraft(toDraft(latest))
  }
  return {
    draft,
    setDraft,
    dirty,
    // 打开时（或跟上的最新）的版本：保存时交回
    version: base.version,
    // 改过的草稿落后于服务端：保存会被拒绝
    stale: latest.version !== base.version,
    discard: () => {
      setBase(latest)
      setDraft(toDraft(latest))
    },
  }
}
