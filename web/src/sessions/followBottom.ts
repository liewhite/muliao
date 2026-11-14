// 会话页的滚动：新内容到来时只在用户本来就在底部时跟随

import { useCallback, useEffect, useRef, useState } from 'react'

// 离底部这么近仍算「在底部」
const PINNED_SLACK_PX = 50

/**
 * 新内容到来时只在用户本来就在底部时跟随；往上翻看历史时不把人拽下来。
 *
 * 「在不在底部」只有一个来源 pinned，除 pin() 外只由用户的滚动改变：滚到离底部 50px 以内就钉住，向上滚离开就解除。不能每次
 * scroll 事件都按距底距离重算——程序滚到底产生的 scroll 事件要到下一帧才派发，那时内容可能又长了，会被误判成
 * 「离开了底部」；而内容变长时 scrollTop 只会不变或变大，不会被当成向上滚。高度变化（ResizeObserver：新数据、
 * 流式输出、图片加载完、输入框变高使容器变矮）时 pinned 为真就滚到底；内容变短时 scrollTop 被钳到最底，距底为 0，
 * 也不会被当成向上滚。打开会话、发送消息就是重新钉住。
 * 代价：在底部展开最后一个折叠块时，页面会跟着滚到它的末尾。
 */
export function useFollowBottom() {
  const scroller = useRef<HTMLElement | null>(null)
  const pinned = useRef(true)
  // null：内容没挂载（加载中），不显示「回到底部」
  const [atBottom, setAtBottom] = useState<boolean | null>(null)
  const content = useRef<HTMLElement | null>(null)
  // 上一次看到的 scrollTop：比它小才是向上滚
  const lastTop = useRef(0)

  const toEnd = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    el.scrollTop = el.scrollHeight
    lastTop.current = el.scrollTop
  }, [])
  const onScroll = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    if (el.scrollHeight - el.scrollTop - el.clientHeight <= PINNED_SLACK_PX) pinned.current = true
    else if (el.scrollTop < lastTop.current) pinned.current = false
    lastTop.current = el.scrollTop
    setAtBottom(pinned.current)
  }, [])
  // 观察滚动容器（输入框变高时它变矮）与内容（新数据、流式输出、图片加载）的尺寸；两个 ref 各自登记，不依赖挂载先后
  const observer = useRef<ResizeObserver | null>(null)
  const resize = useCallback(
    () =>
      (observer.current ??= new ResizeObserver(() => {
        if (pinned.current) toEnd()
      })),
    [toEnd],
  )
  useEffect(() => () => observer.current?.disconnect(), [])

  const scrollRef = useCallback(
    (el: HTMLElement | null) => {
      if (scroller.current !== null) {
        scroller.current.removeEventListener('scroll', onScroll)
        resize().unobserve(scroller.current)
      }
      scroller.current = el
      if (el !== null) {
        el.addEventListener('scroll', onScroll, { passive: true })
        resize().observe(el)
      }
    },
    [onScroll, resize],
  )
  // 内容末尾的锚点：数据加载后才出现，观察它所在的内容容器
  const contentEndRef = useCallback(
    (el: HTMLDivElement | null) => {
      if (content.current !== null) resize().unobserve(content.current)
      content.current = el?.parentElement ?? null
      if (content.current === null) {
        setAtBottom(null)
        return
      }
      resize().observe(content.current)
      setAtBottom(pinned.current)
    },
    [resize],
  )
  /** 回到底部并跟随之后的新内容（打开会话、发送消息、点「回到底部」） */
  const pin = useCallback(() => {
    pinned.current = true
    setAtBottom(true)
    toEnd()
  }, [toEnd])
  return { scrollRef, contentEndRef, atBottom, pin }
}
