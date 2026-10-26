import { findAndReplace } from 'mdast-util-find-and-replace'
import { issueRoute } from './issues'

/**
 * 正文里独立的 `#数字` 是本站 issue 引用。前面紧挨字母、数字、下划线或 & / # 的不算：
 * 外部标号（owner/repo#123）、字符实体（&#123;）、URL 片段；后面紧跟字母数字的也不算（#12abc）。
 */
const ISSUE_REF = /(?<![\p{L}\p{N}_&/#])#(\d+)(?![\p{L}\p{N}_])/gu

/**
 * remark 插件：把文本里的 issue 引用换成指向 issue 详情页的链接。
 * 只处理文本节点，代码块、行内代码、公式本来就不是文本节点；已在链接里的文字（含 GFM 自动链接的 URL）不动。
 */
export function remarkIssueRefs() {
  return (tree: Parameters<typeof findAndReplace>[0]) => {
    findAndReplace(
      tree,
      [
        ISSUE_REF,
        (ref: string, id: string) => ({
          type: 'link',
          url: issueRoute(Number(id)),
          children: [{ type: 'text', value: ref }],
        }),
      ],
      { ignore: ['link', 'linkReference'] },
    )
  }
}
