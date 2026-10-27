"""版本（agents·版本子域，共享内核）在 HTTP 上的输出格式：条目（api/items.py）与记忆（api/memory.py）的历史共用。"""

from datetime import datetime

from pydantic import BaseModel

from muliao.agents.api import RevisionInfo

# 历史接口只列最近这么多版
HISTORY_LIMIT = 50


class RevisionOut(BaseModel):
    rev: int
    # 这一版之后的文档；删除那一版为 null。记忆是正文，skill 是全部文件的 JSON，MCP server 是配置 JSON，secret 只有描述
    content: str | None
    # 这一版之前的文档；新建那一版为 null
    before: str | None
    # "user"，或做出修改的 agent 的 id
    actor: str
    message: str
    created_at: datetime
    # 能恢复到这一版（secret 的版本不含值，删除那一版没有内容）
    restorable: bool

    @classmethod
    def of(cls, r: RevisionInfo) -> "RevisionOut":
        return cls(
            rev=r.id,
            content=r.after,
            before=r.before,
            actor=r.actor,
            message=r.message,
            created_at=r.at,
            restorable=r.restorable,
        )
