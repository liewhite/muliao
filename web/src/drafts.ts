// 编辑草稿与服务端的新值：编辑期间 agent、自进化、别的页面都可能改同一份数据（页面随 changed 事件重新拉取），
// 草稿既不能被新值冲掉，保存时也不能用打开时的旧值盖掉别人的改动（#575、#634）。

import { useState } from 'react'

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

const keysOf = <T extends object>(t: T) => Object.keys(t) as (keyof T)[]

function pick<T extends object>(t: T, keys: (keyof T)[]): Partial<T> {
  return Object.fromEntries(keys.map((k) => [k, t[k]])) as Partial<T>
}

/** 改过的字段：草稿与基线不同 */
export function editedFields<T extends object>(base: T, draft: T): (keyof T)[] {
  return keysOf(draft).filter((k) => !same(draft[k], base[k]))
}

/** 冲突的字段：改过，而服务端此刻的值既不是基线、也不是草稿（编辑期间别处也改了它） */
export function conflictingFields<T extends object>(base: T, draft: T, latest: T): (keyof T)[] {
  return editedFields(base, draft).filter((k) => !same(latest[k], base[k]) && !same(latest[k], draft[k]))
}

/**
 * 服务端有了新值：没改过的字段（草稿等于基线）跟上它；草稿正好等于新值的（多半是自己刚保存的）也跟上。
 * 改过、新值又不同的留着，成为冲突。
 */
export function followed<T extends object>(base: T, draft: T, latest: T): { base: T; draft: T } {
  const next = { base: { ...base }, draft: { ...draft } }
  for (const k of keysOf(latest)) {
    if (same(draft[k], base[k]) || same(draft[k], latest[k])) {
      next.base[k] = latest[k]
      next.draft[k] = latest[k]
    }
  }
  return next
}

/**
 * 编辑表单的草稿：每个字段有基线（开始改它时服务端的值）和草稿，latest 是服务端此刻的值。
 * - 保存提交改过的字段和它们的基线（patch）：服务端此刻的值不是基线就 409（按字段乐观锁，api.fieldlock）。
 *   整份保存、带版本的文档（记忆、skill）交回 latest 的版本：没有冲突时，改过的字段的基线就是 latest 的值。
 * - 冲突（conflicts）由用户逐个决定：用对方的（takeTheirs，草稿换成服务端的值）或覆盖（keepMine，基线换成服务端的值、
 *   草稿留着）。有冲突时不能保存。
 * - 保存成功后 saved(结果)：改过的字段以服务端存下的值（可能规整过）为新的起点。
 */
export function useDraft<T extends object>(latest: T) {
  const [seen, setSeen] = useState(latest)
  const [base, setBase] = useState(latest)
  const [draft, setDraft] = useState(latest)
  if (!same(latest, seen)) {
    setSeen(latest)
    const next = followed(base, draft, latest)
    setBase(next.base)
    setDraft(next.draft)
  }
  const edited = editedFields(base, draft)
  const adopt = (keys: (keyof T)[], from: T, mine: boolean) => {
    setBase((prev) => ({ ...prev, ...pick(from, keys) }))
    if (!mine) setDraft((prev) => ({ ...prev, ...pick(from, keys) }))
  }
  const changes = pick(draft, edited)
  return {
    draft,
    set: (patch: Partial<T>) => setDraft((prev) => ({ ...prev, ...patch })),
    // 改过的字段；要换算后再提交的（如把文字解析成编号）自己从 changes 与 base 拼请求
    changes,
    // 各字段的基线（全部字段）：自己拼请求时取改过的字段；只在打开时读初值的编辑器拿它做 key，基线换了（跟上、用对方的、
    // 保存后）就重新挂载，显示新的草稿
    base,
    // 局部修改的请求体：改过的字段加上它们的基线
    patch: { ...changes, base: pick(base, edited) },
    conflicts: conflictingFields(base, draft, latest),
    latest,
    takeTheirs: (k: keyof T) => adopt([k], latest, false),
    keepMine: (k: keyof T) => adopt([k], latest, true),
    saved: (result: T) => adopt(edited, result, false),
  }
}

export type Draft<T extends object> = ReturnType<typeof useDraft<T>>
