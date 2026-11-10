import { useState } from 'react'

/**
 * 跨刷新记住的界面状态（存在 localStorage 的 key 下）。parse 把存的字符串（没存过时是 null）解析成值：存的值不认识
 * （旧版本留下的、被手改坏的）时由它给出默认值，不在各处各写一遍判断。
 */
export function useStoredState<T>(
  key: string,
  parse: (saved: string | null) => T,
  serialize: (value: T) => string = String,
): readonly [T, (value: T) => void] {
  const [value, setValue] = useState(() => parse(localStorage.getItem(key)))
  const set = (next: T) => {
    localStorage.setItem(key, serialize(next))
    setValue(next)
  }
  return [value, set] as const
}

/** 存的是 options 里的某一个：不是时取 fallback */
export function oneOf<T extends string>(options: readonly T[], fallback: T): (saved: string | null) => T {
  return (saved) => ((options as readonly string[]).includes(saved ?? '') ? (saved as T) : fallback)
}

/** 存的是字符串数组（JSON）：解析不了或形状不对时为空 */
export function parseStringList(saved: string | null): string[] {
  if (saved === null) return []
  let value: unknown
  try {
    value = JSON.parse(saved)
  } catch {
    console.warn('ignoring a malformed stored list', saved)
    return []
  }
  return Array.isArray(value) && value.every((v) => typeof v === 'string') ? value : []
}
