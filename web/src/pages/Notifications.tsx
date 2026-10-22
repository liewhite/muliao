import { useEffect } from 'react'
import { Card } from '@heroui/react'
import { ChevronRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type AppNotification } from '../api'
import { useServerEvents } from '../events'
import { useReturnState } from '../nav'
import { notificationsMayHaveChanged, refreshUnread } from '../unread'
import { EmptyHint, Loading, Page, formatTime, useLoad } from '../ui'

function NotificationCard({ n }: { n: AppNotification }) {
  return (
    <Card className={n.url ? 'transition-colors hover:bg-surface-hover' : undefined}>
      <Card.Content className="flex flex-row items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 font-medium">
            {n.unread && (
              <>
                <span className="size-2 shrink-0 rounded-full bg-accent" aria-hidden />
                <span className="sr-only">未读</span>
              </>
            )}
            <span className="truncate">{n.title}</span>
          </div>
          <div className="mt-1 line-clamp-6 whitespace-pre-wrap break-words text-sm text-muted">{n.body}</div>
          <div className="mt-1 text-xs text-muted">{formatTime(n.created_at)}</div>
        </div>
        {n.url && <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />}
      </Card.Content>
    </Card>
  )
}

export default function Notifications() {
  const from = useReturnState()
  const notifications = useLoad(() => api.notifications(), [])
  useServerEvents((e) => {
    if (notificationsMayHaveChanged(e)) void notifications.reload()
  })

  // 看到了就是已读：只标到这次列出的最新一条，之后才到的保持未读；列表里的未读标记保留到下次加载，让用户看出哪些是新的
  const newest = notifications.data?.find((n) => n.unread)?.id
  useEffect(() => {
    if (newest === undefined) return
    void api
      .markNotificationsRead(newest)
      .then(refreshUnread)
      .catch((e) => console.error('marking notifications read failed', e))
  }, [newest])

  return (
    <Page title="通知">
      <Loading state={notifications}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有通知</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((n) =>
                // 点开打开的页面由服务端给出（与推送一致），返回回到通知页；系统通知没有
                n.url ? (
                  <Link key={n.id} to={n.url} state={from}>
                    <NotificationCard n={n} />
                  </Link>
                ) : (
                  <NotificationCard key={n.id} n={n} />
                ),
              )}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}
