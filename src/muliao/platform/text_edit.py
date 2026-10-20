"""文本的唯一规则（纯函数）：修改文本——替换恰好出现一次的片段，或追加一段（记忆、指令、skill 正文、工作区文件都用它）；
截断过长的文本（交给模型、展示、通知的长输出都用它）。"""

from muliao.errors import InvalidInput


def replace_once(current: str, old: str, new: str, what: str) -> str:
    """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
    count = current.count(old) if old else 0
    if count != 1:
        raise InvalidInput(f"old text must occur exactly once in {what}, found {count} occurrences")
    return current.replace(old, new)


def edit(current: str, old: str, new: str, what: str) -> str:
    """old 恰好出现一次时替换为 new（new 为空即删除）；old 为空时把 new 追加到末尾。"""
    return replace_once(current, old, new, what) if old else append(current, new)


def append(current: str, entry: str) -> str:
    """在末尾追加一段，与已有内容之间空一行。"""
    entry = entry.strip()
    if not entry:
        raise InvalidInput("nothing to append")
    return f"{current.rstrip()}\n\n{entry}\n" if current.strip() else f"{entry}\n"


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…[truncated {len(text) - limit} chars]"
