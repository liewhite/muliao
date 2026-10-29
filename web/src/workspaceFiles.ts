// agent 页「文件」：工作目录浏览的纯函数（路径拼接、按文件名与大小决定怎么预览、高亮用的语言）

/** 超过它的文件不预览，只给下载 */
export const MAX_PREVIEW_BYTES = 1_000_000
/** 超过它的文本不做语法高亮（高亮在主线程同步执行） */
export const MAX_HIGHLIGHT_BYTES = 200_000

export type PreviewKind = 'image' | 'markdown' | 'text' | 'too-big'

/** 目录里的一项的相对路径（目录为空串是工作目录本身） */
export const joinPath = (dir: string, name: string) => (dir ? `${dir}/${name}` : name)

/** joinPath 的反向：[所在目录, 名称] */
export function splitPath(path: string): [string, string] {
  const i = path.lastIndexOf('/')
  return i < 0 ? ['', path] : [path.slice(0, i), path.slice(i + 1)]
}

const ext = (name: string) => {
  const i = name.lastIndexOf('.')
  return i > 0 ? name.slice(i + 1).toLowerCase() : ''
}

/** 预览方式：能内联的图片由后端判定（inline）；其余按大小与扩展名，不认识的先按文本读，读到二进制内容再改为只给下载（isBinary） */
export function previewKind(entry: { name: string; size: number | null; inline: boolean }): PreviewKind {
  if (entry.inline) return 'image'
  if ((entry.size ?? 0) > MAX_PREVIEW_BYTES) return 'too-big'
  const e = ext(entry.name)
  return e === 'md' || e === 'markdown' ? 'markdown' : 'text'
}

/** 解码出的文字里有 NUL 即是二进制文件 */
export const isBinary = (text: string) => text.includes('\u0000')

// 扩展名（或无扩展名的文件名）→ highlight.js 的语言名；都在 highlight.js/lib/common 里
const LANGUAGES: Record<string, string> = {
  py: 'python',
  ts: 'typescript',
  tsx: 'typescript',
  mts: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  json: 'json',
  jsonl: 'json',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  yaml: 'yaml',
  yml: 'yaml',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  css: 'css',
  scss: 'scss',
  html: 'xml',
  htm: 'xml',
  xml: 'xml',
  svg: 'xml',
  plist: 'xml',
  sql: 'sql',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'kotlin',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  rb: 'ruby',
  php: 'php',
  lua: 'lua',
  diff: 'diff',
  patch: 'diff',
  makefile: 'makefile',
}

/** 高亮用的语言；认不出为 null（按纯文本显示，不做自动识别：慢且常认错） */
export function languageOf(name: string): string | null {
  return LANGUAGES[ext(name) || name.toLowerCase()] ?? null
}

/** 文件大小的简短写法 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
