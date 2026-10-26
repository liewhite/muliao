import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
  type Ref,
} from 'react'
import {
  Alert,
  AlertDialog,
  Button,
  Card,
  Chip,
  Description,
  Disclosure,
  Input,
  Label,
  ListBox,
  Select,
  Spinner,
  Switch,
  TextArea,
  TextField,
  ToggleButton,
  ToggleButtonGroup,
  buttonVariants,
  toast,
  type ButtonProps,
  type Key,
} from '@heroui/react'
import { ChevronLeft, Menu, Plus } from 'lucide-react'
import { backTarget, useOpenAdminMenu } from './nav'
import ReactMarkdown, { type Components } from 'react-markdown'
import { Link as RouterLink, useLocation, useNavigate } from 'react-router-dom'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import 'katex/dist/katex.min.css'
import { FILES_PATH, api, type Agent, type AgentId } from './api'
import { listenServerEvents, shouldRefetch } from './events'
import { normalizeMath } from './markdown'
import { isSendShortcut, SEND_HINT } from './sendKey'

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

// ---- 数据加载与操作 ----

export interface Loaded<T> {
  data: T | undefined
  error: string | null
  loading: boolean
  reload: () => Promise<void>
}

/**
 * 加载数据；deps 变化时重新加载。错误原样展示，不吞掉。
 * data 只来自当前 deps 的请求：deps 一变就清空旧数据，晚到的旧请求结果直接丢弃。
 */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  // 每次请求的序号：只有最新一次请求的结果能落到状态里
  const latest = useRef(0)
  const run = useCallback(load, deps)
  const reload = useCallback(async () => {
    const id = ++latest.current
    try {
      const value = await run()
      if (id !== latest.current) return
      setData(value)
      setError(null)
    } catch (e) {
      if (id !== latest.current) return
      setError(errorMessage(e))
    } finally {
      if (id === latest.current) setLoading(false)
    }
  }, [run])
  useEffect(() => {
    setData(undefined)
    setError(null)
    setLoading(true)
    void reload()
    // 换参数或卸载后，在途请求一律作废
    return () => {
      // 这是请求序号计数器而不是 DOM 引用，清理时读最新值正是本意
      // oxlint-disable-next-line react-hooks/exhaustive-deps
      latest.current++
    }
  }, [reload])
  return { data, error, loading, reload }
}

/** 包装一个用户操作：执行中 busy，失败时记录错误（由 ErrorAlert 展示）。返回是否成功。 */
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = useCallback(async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      return true
    } catch (e) {
      setError(errorMessage(e))
      return false
    } finally {
      setBusy(false)
    }
  }, [])
  return { busy, error, run, setError }
}

// agent 列表的共享缓存：选择器、id→名称显示等各处共用一份，不各自请求。有组件在用时订阅服务端事件，
// shouldRefetch 时重新拉取；没人用了就退订，下次有人用时先显示上次的结果、同时重新拉取
type AgentsState = Omit<Loaded<Agent[]>, 'reload'>
let agentsState: AgentsState = { data: undefined, error: null, loading: true }
let agentsLatest = 0
let agentsUnlisten: (() => void) | null = null
const agentsSubscribers = new Set<() => void>()

async function reloadAgents(): Promise<void> {
  // 只让最新一次请求的结果落下，晚到的旧响应不能覆盖新值
  const id = ++agentsLatest
  let next: AgentsState
  try {
    next = { data: await api.agents(), error: null, loading: false }
  } catch (e) {
    next = { ...agentsState, error: errorMessage(e), loading: false }
  }
  if (id !== agentsLatest) return
  agentsState = next
  agentsSubscribers.forEach((s) => s())
}

function subscribeAgents(onChange: () => void) {
  agentsSubscribers.add(onChange)
  if (agentsUnlisten === null) {
    agentsUnlisten = listenServerEvents((e) => {
      if (shouldRefetch(e, 'agents')) void reloadAgents()
    })
    void reloadAgents()
  }
  return () => {
    agentsSubscribers.delete(onChange)
    if (agentsSubscribers.size === 0 && agentsUnlisten !== null) {
      agentsUnlisten()
      agentsUnlisten = null
    }
  }
}

/** agent 列表（选择器与 id→名称显示共用，见上面的共享缓存）。 */
export function useAgents(): Loaded<Agent[]> {
  return { ...useSyncExternalStore(subscribeAgents, () => agentsState), reload: reloadAgents }
}

/** 按 id 显示 agent 名称；agent 列表尚未加载完时先显示 id。 */
export function agentLabel(agents: Agent[] | undefined, id: AgentId): string {
  return agents?.find((a) => a.id === id)?.name ?? id
}

// ---- 布局 ----

export function Page({
  title,
  back,
  actions,
  footer,
  scrollRef,
  wide = false,
  children,
}: {
  title: ReactNode
  /** 内容不限宽、不加内边距（如横向滚动的看板），由页面自己排版 */
  wide?: boolean
  back?: string
  actions?: ReactNode
  /** 固定在页面底部、不随内容滚动（如输入框） */
  footer?: ReactNode
  /** 页面内容的滚动容器（需要跟随滚动的页面用，如会话页） */
  scrollRef?: Ref<HTMLElement>
  children: ReactNode
}) {
  const navigate = useNavigate()
  const { state } = useLocation()
  const openAdminMenu = useOpenAdminMenu()
  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-2 border-b border-separator bg-surface px-3 pt-[calc(env(safe-area-inset-top)+0.5rem)] pb-2">
        {back !== undefined ? (
          <Button isIconOnly variant="ghost" size="sm" aria-label="返回" onPress={() => navigate(backTarget(state, back))}>
            <ChevronLeft className="size-5" />
          </Button>
        ) : (
          // 一级页面的左上角是管理菜单；桌面端左侧栏已有同一个按钮
          <Button isIconOnly variant="ghost" size="sm" aria-label="管理菜单" className="md:hidden" onPress={openAdminMenu}>
            <Menu className="size-5" />
          </Button>
        )}
        <h1 className="min-w-0 flex-1 truncate text-lg font-semibold">{title}</h1>
        <div className="flex items-center gap-2">{actions}</div>
      </header>
      <main ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
        {wide ? children : <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-3">{children}</div>}
      </main>
      {footer}
    </div>
  )
}

/** 按钮样式的路由链接（HeroUI 的 Button 不带 href）。 */
export function LinkButton({
  to,
  variant = 'primary',
  size = 'sm',
  children,
}: {
  to: string
  variant?: 'primary' | 'secondary' | 'tertiary' | 'outline' | 'ghost'
  size?: 'sm' | 'md' | 'lg'
  children: ReactNode
}) {
  return (
    <RouterLink to={to} className={buttonVariants({ variant, size })}>
      {children}
    </RouterLink>
  )
}

/** 带「处理中」转圈的按钮：HeroUI 的 isPending 只禁用交互，不提供视觉反馈。 */
export function ActionButton({ isPending = false, children, ...props }: ButtonProps & { children: ReactNode }) {
  return (
    <Button isPending={isPending} {...props}>
      {isPending && <Spinner size="sm" color="current" />}
      {children}
    </Button>
  )
}

/** 列表里可折叠的一组（如一个 agent 的触发器、一个分组的 agent）。组头靠左，左侧一道强调色竖条 + 加粗大字，和卡片区分开 */
export function GroupSection({
  name,
  count,
  defaultExpanded = true,
  children,
}: {
  name: string
  count: number
  defaultExpanded?: boolean
  children: ReactNode
}) {
  return (
    <Disclosure defaultExpanded={defaultExpanded}>
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 border-l-4 border-accent py-1 pl-2 text-base font-semibold">
          {/* HeroUI 的指示箭头自带 margin-inline-start:auto（默认放最右），这里要紧跟在左边 */}
          <Disclosure.Indicator className="ms-0" />
          <span className="truncate">{name}</span>
          <Chip size="sm" variant="soft">
            {count}
          </Chip>
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-2">{children}</Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

/** 卡片里可折叠的「新建 xxx」表单。 */
export function CollapsibleForm({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card>
      <Disclosure>
        <Disclosure.Heading>
          <Disclosure.Trigger className="flex w-full items-center gap-2 px-1 font-medium">
            <Plus className="size-4" />
            {title}
            <Disclosure.Indicator className="ml-auto" />
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="flex flex-col gap-4 pt-4">{children}</Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>
    </Card>
  )
}

/** 开关。label 描述它控制什么（读屏名称），状态由开关本身表达。 */
export function ToggleSwitch({
  label,
  isSelected,
  onChange,
  isDisabled,
}: {
  label: string
  isSelected: boolean
  onChange: (value: boolean) => void
  isDisabled?: boolean
}) {
  return (
    <Switch size="sm" isSelected={isSelected} onChange={onChange} isDisabled={isDisabled}>
      <Switch.Content>
        <Switch.Control>
          <Switch.Thumb />
        </Switch.Control>
        <Label className="sr-only">{label}</Label>
      </Switch.Content>
    </Switch>
  )
}

/** 代码/日志块的统一样式。 */
export const CODE_BLOCK =
  'my-1 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-surface-secondary p-2 font-mono text-xs text-foreground'

// ---- 状态展示 ----

export function ErrorAlert({ error, title = '出错了' }: { error: string | null; title?: string }) {
  if (!error) return null
  return (
    <Alert status="danger">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>{title}</Alert.Title>
        <Alert.Description className="whitespace-pre-wrap break-words">{error}</Alert.Description>
      </Alert.Content>
    </Alert>
  )
}

/**
 * 首次加载失败才整页报错；已有数据时重新拉取失败（网络抖动等）照常显示已有内容，只在顶部提示错误，
 * 下次重新拉取成功后提示自动消失。
 */
export function Loading<T>({ state, children }: { state: Loaded<T>; children: (data: T) => ReactNode }) {
  if (state.data !== undefined && state.error) {
    return (
      <>
        <ErrorAlert error={state.error} title="重新加载失败，显示的是之前的内容" />
        {children(state.data)}
      </>
    )
  }
  if (state.error) return <ErrorAlert error={state.error} title="加载失败" />
  if (state.data === undefined) {
    return (
      <div className="flex justify-center py-10">
        <Spinner />
      </div>
    )
  }
  return <>{children(state.data)}</>
}

export function EmptyHint({ children }: { children: ReactNode }) {
  return <p className="py-10 text-center text-sm text-muted">{children}</p>
}

/** 只读列出继承来的公共资源（见 ownership.ts；是否被遮蔽由后端给出）：私有视角里能看到它们，但只能在设置页管理。 */
export function InheritedSection({ title, to, children }: { title: string; to: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-muted">{title}</h2>
        <RouterLink to={to} className="shrink-0 text-sm text-accent">
          去设置页管理
        </RouterLink>
      </div>
      {children}
    </section>
  )
}

const isFileUrl = (src: string) => src.startsWith(`${FILES_PATH}/`)

// 只渲染本站文件存储里的图片：外链图片会在打开会话时由浏览器自动请求，被注入的模型可以借它把对话内容
// 拼进 URL 发出去，所以显示成普通链接，由用户决定是否点开
const MARKDOWN_COMPONENTS: Components = {
  img: ({ src, alt }) => {
    const url = typeof src === 'string' ? src : ''
    if (isFileUrl(url)) {
      return (
        <a href={url} target="_blank" rel="noreferrer">
          <img src={url} alt={alt ?? ''} loading="lazy" className="max-h-96 rounded-lg" />
        </a>
      )
    }
    return (
      <a href={url} target="_blank" rel="noreferrer noopener">
        {alt || url}
      </a>
    )
  },
  // 只有外部的绝对地址在新标签页打开；页内锚点（如 GFM 脚注）与本站文件保持原样，属性（脚注的 id、data-*）照传
  a: ({ node: _node, ...props }) =>
    /^https?:/i.test(props.href ?? '') ? <a {...props} target="_blank" rel="noreferrer noopener" /> : <a {...props} />,
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="prose prose-sm max-w-none break-words dark:prose-invert prose-pre:overflow-x-auto">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]} components={MARKDOWN_COMPONENTS}>
        {normalizeMath(text)}
      </ReactMarkdown>
    </div>
  )
}

// ---- 表单字段 ----
// 表单都放在卡片或底部栏（surface 背景）上；primary 变体的字段背景与 surface 同色，看不出边界，所以统一用 secondary

interface FieldProps {
  label: string
  hint?: ReactNode
  value: string
  onChange: (value: string) => void
  isRequired?: boolean
  isDisabled?: boolean
  autoFocus?: boolean
}

export function TextInput({
  label,
  hint,
  type,
  placeholder,
  mono,
  autoFocus,
  ...field
}: FieldProps & {
  type?: 'text' | 'password' | 'number' | 'datetime-local'
  placeholder?: string
  mono?: boolean
}) {
  return (
    <TextField type={type} fullWidth variant="secondary" {...field}>
      <Label>{label}</Label>
      <Input placeholder={placeholder} className={mono ? 'font-mono' : undefined} autoFocus={autoFocus} />
      {hint && <Description>{hint}</Description>}
    </TextField>
  )
}

export function TextAreaInput({
  label,
  hint,
  rows = 4,
  mono,
  autoFocus,
  onSubmit,
  ...field
}: FieldProps & {
  rows?: number
  mono?: boolean
  /** 有它时 ⌘+Enter（Ctrl+Enter）调用它提交，Enter 照常换行；调用方自己判断当前能否提交 */
  onSubmit?: () => void
}) {
  return (
    <TextField fullWidth variant="secondary" {...field}>
      <Label>{label}</Label>
      <TextArea
        rows={rows}
        className={mono ? 'font-mono text-sm' : undefined}
        autoFocus={autoFocus}
        placeholder={onSubmit ? SEND_HINT : undefined}
        onKeyDown={
          onSubmit &&
          ((e) => {
            if (isSendShortcut(e)) {
              e.preventDefault()
              onSubmit()
            }
          })
        }
      />
      {hint && <Description>{hint}</Description>}
    </TextField>
  )
}

export interface Option {
  id: string
  label: string
  description?: string
  /** 显示在标签前的标记（如负责项目的色点） */
  prefix?: ReactNode
}

export function SelectInput({
  label,
  hint,
  value,
  onChange,
  options,
  placeholder = '请选择',
  isDisabled,
}: {
  label: string
  hint?: ReactNode
  /** null 表示未选择（显示 placeholder）；选项 id 都是非空字符串 */
  value: string | null
  onChange: (value: string) => void
  options: Option[]
  placeholder?: string
  isDisabled?: boolean
}) {
  return (
    <Select
      fullWidth
      variant="secondary"
      placeholder={placeholder}
      value={value}
      onChange={(key: Key | null) => {
        // 单选且无清除按钮：选中后不会回到未选择
        if (key === null) throw new Error(`select "${label}" was cleared unexpectedly`)
        onChange(String(key))
      }}
      isDisabled={isDisabled}
    >
      <Label>{label}</Label>
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      {hint && <Description>{hint}</Description>}
      <Select.Popover>
        <ListBox>
          {options.map((o) => (
            <ListBox.Item key={o.id} id={o.id} textValue={o.label}>
              <div className="flex flex-col">
                <span className="flex items-center gap-2">
                  {o.prefix}
                  {o.label}
                </span>
                {o.description && <span className="text-xs text-muted">{o.description}</span>}
              </div>
              <ListBox.ItemIndicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  )
}

/** 单选切换（如列表筛选）。 */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  label,
  showLabel = false,
}: {
  value: T
  onChange: (value: T) => void
  options: readonly { id: T; label: string }[]
  label: string
  /** 显示可见标签（表单中）；否则 label 只作为读屏名称 */
  showLabel?: boolean
}) {
  const labelId = useId()
  return (
    <div className="flex flex-col gap-1">
      {showLabel && (
        <span id={labelId} className="text-sm font-medium">
          {label}
        </span>
      )}
      <ToggleButtonGroup
        {...(showLabel ? { 'aria-labelledby': labelId } : { 'aria-label': label })}
        size="sm"
        selectionMode="single"
        disallowEmptySelection
        selectedKeys={[value]}
        onSelectionChange={(keys) => {
          // disallowEmptySelection 保证恰好选中一项
          const [key] = [...keys]
          if (key === undefined) throw new Error(`segmented "${label}" lost its selection`)
          onChange(String(key) as T)
        }}
      >
        {options.map((o) => (
          <ToggleButton key={o.id} id={o.id}>
            {o.label}
          </ToggleButton>
        ))}
      </ToggleButtonGroup>
    </div>
  )
}

// ---- 确认弹窗 ----

interface ConfirmOptions {
  title: string
  body?: ReactNode
  confirmLabel?: string
  danger?: boolean
}

type Confirm = (options: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<Confirm | null>(null)

/** 全局唯一的确认弹窗；页面通过 useConfirm() 以 Promise 形式询问用户。 */
export function ConfirmProvider({ children }: { children: ReactNode }) {
  // options 在关闭后保留，避免退场动画期间内容和按钮样式闪变
  const [options, setOptions] = useState<ConfirmOptions | null>(null)
  const [isOpen, setOpen] = useState(false)
  const resolver = useRef<((ok: boolean) => void) | null>(null)
  const { pathname } = useLocation()

  const settle = useCallback((ok: boolean) => {
    resolver.current?.(ok)
    resolver.current = null
    setOpen(false)
  }, [])

  const confirm = useCallback<Confirm>(
    (opts) => {
      // 新的询问会让尚未回答的旧询问以「取消」结束
      settle(false)
      setOptions(opts)
      setOpen(true)
      return new Promise<boolean>((resolve) => {
        resolver.current = resolve
      })
    },
    [settle],
  )

  // 弹窗属于发起它的页面：页面切走（通知跳转、返回、登录失效）即视为取消，不能对旧页面执行操作
  useEffect(() => settle(false), [pathname, settle])

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AlertDialog.Backdrop isOpen={isOpen} onOpenChange={(open) => !open && settle(false)}>
        <AlertDialog.Container size="sm">
          <AlertDialog.Dialog>
            <AlertDialog.Header>
              <AlertDialog.Icon status={options?.danger ? 'danger' : 'accent'} />
              <AlertDialog.Heading>{options?.title}</AlertDialog.Heading>
            </AlertDialog.Header>
            {options?.body && <AlertDialog.Body>{options.body}</AlertDialog.Body>}
            <AlertDialog.Footer>
              <Button variant="tertiary" onPress={() => settle(false)}>
                取消
              </Button>
              <Button variant={options?.danger ? 'danger' : 'primary'} onPress={() => settle(true)}>
                {options?.confirmLabel ?? '确定'}
              </Button>
            </AlertDialog.Footer>
          </AlertDialog.Dialog>
        </AlertDialog.Container>
      </AlertDialog.Backdrop>
    </ConfirmContext.Provider>
  )
}

export function useConfirm(): Confirm {
  const confirm = useContext(ConfirmContext)
  if (!confirm) throw new Error('useConfirm must be used inside <ConfirmProvider>')
  return confirm
}

// ---- 时间 ----

const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

export function formatTime(iso: string): string {
  return dateFormat.format(new Date(iso))
}

export function relativeTime(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return formatTime(iso)
}

/** 复制到剪贴板。剪贴板 API 只在 HTTPS 等安全上下文可用；不可用时如实报错，文本本身可直接选中复制 */
export async function copyText(text: string, what: string): Promise<void> {
  if (!navigator.clipboard) throw new Error(`当前页面无法访问剪贴板（需要 HTTPS），请手动选中${what}复制`)
  await navigator.clipboard.writeText(text)
  toast.success(`已复制${what}`)
}
