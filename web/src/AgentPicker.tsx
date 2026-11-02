import { useId, useState } from 'react'
import { Dropdown, Label, SearchField } from '@heroui/react'
import { selectVariants } from '@heroui/styles'
import { ChevronDown, Folder } from 'lucide-react'
import { Autocomplete } from 'react-aria-components'
import { groupAgents, searchAgents, selectableAgents } from './agentGroups'
import type { Agent } from './api'

/**
 * 选 agent：一级菜单是各分组（悬停/点击展开二级列表）和未分组的 agent；
 * 有搜索词时不分级，平铺按名称/描述匹配到的 agent 并标出分组。描述只显示一行，完整内容在 title 提示里。
 * agents 传完整列表：菜单只列可选的（selectableAgents）；当前值归档了（如从它的详情页带 ?agent= 进来）仍显示在按钮上，由服务端在使用时拒绝并说明。
 */
export function AgentPicker({
  label,
  agents,
  value,
  onChange,
  placeholder = '选择 agent',
}: {
  label: string
  agents: Agent[]
  /** null 表示未选择 */
  value: string | null
  onChange: (id: string) => void
  placeholder?: string
}) {
  const labelId = useId()
  const valueId = useId()
  const [query, setQuery] = useState('')
  const current = agents.find((a) => a.id === value)
  const active = selectableAgents(agents)
  const selected = value === null ? [] : [value]
  const searching = query.trim() !== ''
  const slots = selectVariants({ variant: 'secondary', fullWidth: true })

  function choose(id: string) {
    onChange(id)
    setQuery('')
  }

  return (
    <div className={slots.base()}>
      <Label id={labelId}>{label}</Label>
      <Dropdown onOpenChange={(open) => !open && setQuery('')}>
        <Dropdown.Trigger aria-labelledby={`${labelId} ${valueId}`} className={slots.trigger()}>
          <span id={valueId} className={`${slots.value()} flex min-w-0 items-center gap-2`}>
            {current ? (
              <>
                <span className="truncate">{current.name}</span>
                {current.group !== null && <span className="shrink-0 text-xs text-muted">{current.group}</span>}
              </>
            ) : (
              <span className="text-muted">{placeholder}</span>
            )}
          </span>
          <ChevronDown className={slots.indicator()} />
        </Dropdown.Trigger>
        <Dropdown.Popover placement="bottom start" className="w-[min(28rem,90vw)]">
          <Autocomplete inputValue={query} onInputChange={setQuery}>
            <SearchField aria-label="搜索 agent" autoFocus fullWidth variant="secondary" className="p-2">
              <SearchField.Group>
                <SearchField.SearchIcon />
                <SearchField.Input placeholder="搜索名称或描述" />
                <SearchField.ClearButton />
              </SearchField.Group>
            </SearchField>
            <Dropdown.Menu
              aria-label={label}
              className="max-h-96 overflow-y-auto"
              selectionMode="single"
              selectedKeys={selected}
              renderEmptyState={() => <EmptyMenu />}
            >
              {searching ? (
                searchAgents(active, query).map((a) => <AgentItem key={a.id} agent={a} showGroup onChoose={choose} />)
              ) : (
                <AgentTree agents={active} selected={selected} onChoose={choose} />
              )}
            </Dropdown.Menu>
          </Autocomplete>
        </Dropdown.Popover>
      </Dropdown>
    </div>
  )
}

/** 未搜索时的级联菜单：分组在前（展开为二级菜单），未分组的 agent 与分组同级排在后面 */
function AgentTree({
  agents,
  selected,
  onChoose,
}: {
  agents: Agent[]
  selected: string[]
  onChoose: (id: string) => void
}) {
  const { groups, ungrouped } = groupAgents(agents)
  return (
    <>
      {groups.map((g) => (
        <Dropdown.SubmenuTrigger key={`group:${g.name}`}>
          <Dropdown.Item id={`group:${g.name}`} textValue={g.name}>
            <Folder className="size-4 shrink-0 text-muted" />
            <span className="flex-1 truncate">{g.name}</span>
            <span className="text-xs text-muted">{g.agents.length}</span>
            <Dropdown.SubmenuIndicator />
          </Dropdown.Item>
          <Dropdown.Popover className="w-[min(24rem,85vw)]">
            <Dropdown.Menu
              aria-label={g.name}
              className="max-h-96 overflow-y-auto"
              selectionMode="single"
              selectedKeys={selected}
            >
              {g.agents.map((a) => (
                <AgentItem key={a.id} agent={a} onChoose={onChoose} />
              ))}
            </Dropdown.Menu>
          </Dropdown.Popover>
        </Dropdown.SubmenuTrigger>
      ))}
      {ungrouped.map((a) => (
        <AgentItem key={a.id} agent={a} onChoose={onChoose} />
      ))}
    </>
  )
}

function AgentItem({
  agent,
  showGroup,
  onChoose,
}: {
  agent: Agent
  showGroup?: boolean
  onChoose: (id: string) => void
}) {
  return (
    <Dropdown.Item id={agent.id} textValue={agent.name} onAction={() => onChoose(agent.id)}>
      <div className="flex min-w-0 flex-1 flex-col" title={agent.description || undefined}>
        <span className="flex items-center gap-2">
          <span className="truncate">{agent.name}</span>
          {showGroup && agent.group !== null && <span className="shrink-0 text-xs text-muted">{agent.group}</span>}
        </span>
        {agent.description && <span className="truncate text-xs text-muted">{agent.description}</span>}
      </div>
      <Dropdown.ItemIndicator />
    </Dropdown.Item>
  )
}

function EmptyMenu() {
  return <div className="p-2 text-sm text-muted">没有匹配的 agent</div>
}
