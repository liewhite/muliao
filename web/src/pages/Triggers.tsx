import { useState } from 'react'
import { Tabs, type Key } from '@heroui/react'
import { Page } from '../ui'
import { CronPanel } from './Cron'
import { EventTriggerPanel } from './EventTriggers'

/** 自动发起会话的入口：定时（cron）与外部事件 */
export default function Triggers() {
  const [tab, setTab] = useState<Key>('cron')
  return (
    <Page title="触发">
      <Tabs selectedKey={tab} onSelectionChange={setTab} variant="secondary">
        <Tabs.ListContainer>
          <Tabs.List aria-label="触发方式">
            <Tabs.Tab id="cron">
              定时
              <Tabs.Indicator />
            </Tabs.Tab>
            <Tabs.Tab id="event">
              事件
              <Tabs.Indicator />
            </Tabs.Tab>
          </Tabs.List>
        </Tabs.ListContainer>
        <Tabs.Panel id="cron" className="pt-3">
          <CronPanel />
        </Tabs.Panel>
        <Tabs.Panel id="event" className="pt-3">
          <EventTriggerPanel />
        </Tabs.Panel>
      </Tabs>
    </Page>
  )
}
