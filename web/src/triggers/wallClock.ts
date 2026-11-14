// 「挂钟时间」：不带时区的本地时间 YYYY-MM-DDTHH:mm，正是 <input type="datetime-local"> 的值。
// 一次性触发器的时间按这个约定传给后端，由后端按服务端配置的 timezone 解释（与 cron 一致）。

/** 后端存回来的时间带着服务端时区的偏移，前 16 个字符就是那边的挂钟时间，正是输入框要显示的 */
export const wallClock = (at: string) => at.slice(0, 16)

/** 新建一次性触发器时的默认时间：浏览器本地时间一小时后 */
export function inAnHour(now: Date): string {
  const at = new Date(now.getTime() + 3600_000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`
}
