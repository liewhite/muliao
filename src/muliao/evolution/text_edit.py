"""agent 修改自己文本资源（记忆、指令）的唯一规则：替换恰好出现一次的片段，或追加一段。纯函数。"""

from muliao.errors import InvalidInput


def replace_once(current: str, old: str, new: str, what: str) -> str:
    """把恰好出现一次的 old 替换为 new；new 为空即删除。"""
    count = current.count(old) if old else 0
    if count != 1:
        raise InvalidInput(f"old text must occur exactly once in {what}, found {count} occurrences")
    return current.replace(old, new)


def append(current: str, entry: str) -> str:
    """在末尾追加一段，与已有内容之间空一行。"""
    entry = entry.strip()
    if not entry:
        raise InvalidInput("nothing to append")
    return f"{current.rstrip()}\n\n{entry}\n" if current.strip() else f"{entry}\n"
