// 用量视图的纯逻辑（页面在 pages/Usage.tsx）

/**
 * 最近 days 个自然日：从 days-1 天前的**浏览器本地**零点起，到此刻。
 * 按天的分组由服务端按它配置的时区划分：两边时区一致时（单用户的常态）每一条都是整天；
 * 不一致时只是首尾两天不完整，总量不受影响。
 */
export function windowOf(days: number, now: Date = new Date()): [Date, Date] {
  const since = new Date(now)
  since.setHours(0, 0, 0, 0)
  since.setDate(since.getDate() - (days - 1))
  return [since, now]
}

/** token 数量按千/百万缩写：几十万的数字逐位读没有意义 */
export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 10_000) return `${Math.round(n / 1000)}K`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

/** 缓存命中比例（整数百分比）。input 是输入总数，缓存读是其中一部分；没有输入时为 0 */
export function cacheHitPercent(inputTokens: number, cacheReadTokens: number): number {
  return inputTokens === 0 ? 0 : Math.round((cacheReadTokens / inputTokens) * 100)
}
