"""会话来源（Origin）：会话创建时定下、整个会话不变（存在 sessions.origin），以及每种来源在指令里怎么向 agent 说明。
说明是会话级的稳定内容，进 instructions（缓存前缀）：会话领域把它作为文字交给 agent_runtime，运行时不认识来源。

会话领域只写自己开出的来源的说明（OWN_NOTES：用户、委派）；触发器、issue 的说明含它们的规则（怎么处理一个 issue），
由各自领域的 api 导出（SESSION_NOTE），组合根合成 OriginNotes 交给会话领域。"""

from collections.abc import Mapping
from enum import StrEnum


class Origin(StrEnum):
    """会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派、issue 看板唤醒被指派的 agent。"""

    USER = "user"
    TRIGGER = "trigger"
    DELEGATION = "delegation"
    ISSUE = "issue"


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
