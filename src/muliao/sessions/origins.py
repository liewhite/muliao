"""会话来源（Origin）：会话创建时定下、整个会话不变（存在 sessions.origin），以及每种来源在指令里怎么向 agent 说明。
说明是会话级的稳定内容，进 instructions（缓存前缀）：会话领域把它作为文字交给 agent_runtime，运行时不认识来源。

会话领域只写自己开出的来源的说明（OWN_NOTES：用户、委派）；触发器、issue 的说明含它们的规则（怎么处理一个 issue），
由各自领域的 api 导出（SESSION_NOTE），组合根合成 OriginNotes 交给会话领域。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Origin(StrEnum):
    """会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派、issue 看板唤醒被指派的 agent。"""

    USER = "user"
    TRIGGER = "trigger"
    DELEGATION = "delegation"
    ISSUE = "issue"


@dataclass(frozen=True)
class OriginRule:
    """一种来源在来信规则里的差别，只在这张表里写（sessions.inbound 读它，不按来源分支）。"""

    # 发送方有自己的身份（origin_ref：触发器 id、issue id）：经 SenderMail 投信，能按身份定位会话（SessionKey）
    identified: bool
    # 发送方是父会话（委派）：会话的发起方记在 parent_session_id 而不是 origin_ref，只有它开 thread
    from_parent: bool
    # 能在任何用户能写的会话里续写（用户）；否则只续写自己发起的
    writes_anywhere: bool


RULES: dict[Origin, OriginRule] = {
    Origin.USER: OriginRule(identified=False, from_parent=False, writes_anywhere=True),
    Origin.TRIGGER: OriginRule(identified=True, from_parent=False, writes_anywhere=False),
    Origin.DELEGATION: OriginRule(identified=False, from_parent=True, writes_anywhere=False),
    Origin.ISSUE: OriginRule(identified=True, from_parent=False, writes_anywhere=False),
}


OWN_NOTES: dict[Origin, str] = {
    Origin.USER: "The user started this conversation in the app and may be reading along.",
    Origin.DELEGATION: "Another agent opened this conversation with you.",
}


class OriginNotes:
    """每种来源的会话在指令「Runtime context」里的那一行说明。构造时必须覆盖全部来源，缺了当场报错。"""

    def __init__(self, notes: Mapping[Origin, str]):
        missing = [o.value for o in Origin if o not in notes]
        if missing:
            raise RuntimeError(f"no session note for origins {missing}")
        self._notes = dict(notes)

    def __getitem__(self, origin: Origin) -> str:
        return self._notes[origin]
