"""会话来源（Origin）：会话创建时定下、整个会话不变（存在 sessions.origin），以及每种来源在指令里怎么向 agent 说明。

来源编号的格式归这里：库里存两个字符串（sessions.origin、origin_ref），对外只有结构化的来源 SessionOrigin，
发送方交出自己的 id（TriggerOrigin、IssueOrigin），读回经 origin_of 一处解析，别的领域不拼、不解这个字符串。
说明是会话级的稳定内容，进 instructions（缓存前缀）：会话领域把它作为文字交给 agent_runtime，运行时不认识来源。

会话领域只写自己开出的来源的说明（OWN_NOTES：用户、委派）；触发器、issue 的说明含它们的规则（怎么处理一个 issue），
由各自领域的装配对象声明（origin_notes），组合根合成 OriginNotes 交给会话领域。"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from muliao.errors import CorruptState


class Origin(StrEnum):
    """会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派、issue 看板唤醒被指派的 agent。"""

    USER = "user"
    TRIGGER = "trigger"
    DELEGATION = "delegation"
    ISSUE = "issue"


@dataclass(frozen=True)
class OriginRule:
    """一种来源在来信规则里的差别，只在这张表里写（sessions.inbound 读它，不按来源分支）。"""

    # 能在任何用户能写的会话里续写（用户）；否则只续写自己发起的
    writes_anywhere: bool


RULES: dict[Origin, OriginRule] = {
    Origin.USER: OriginRule(writes_anywhere=True),
    Origin.TRIGGER: OriginRule(writes_anywhere=False),
    Origin.DELEGATION: OriginRule(writes_anywhere=False),
    Origin.ISSUE: OriginRule(writes_anywhere=False),
}


# kind 有默认值（构造时不必写），接口描述里仍是必有的字段：前端按它区分种类
_ORIGIN_CONFIG = ConfigDict(frozen=True, extra="forbid", json_schema_serialization_defaults_required=True)


class UserOrigin(BaseModel):
    model_config = _ORIGIN_CONFIG
    kind: Literal[Origin.USER] = Origin.USER


class DelegationOrigin(BaseModel):
    """发起方是父会话（存在会话的 parent_session_id，不在来源里另存一份）。"""

    model_config = _ORIGIN_CONFIG
    kind: Literal[Origin.DELEGATION] = Origin.DELEGATION


class TriggerOrigin(BaseModel):
    model_config = _ORIGIN_CONFIG
    kind: Literal[Origin.TRIGGER] = Origin.TRIGGER
    trigger_id: int

    @property
    def ref(self) -> str:
        return str(self.trigger_id)


class IssueOrigin(BaseModel):
    model_config = _ORIGIN_CONFIG
    kind: Literal[Origin.ISSUE] = Origin.ISSUE
    issue_id: int

    @property
    def ref(self) -> str:
        return str(self.issue_id)


# 有自己身份的发送方（别的领域）：经 SenderMail 投信、能按身份定位会话（SessionKey）；在库里的编号是 ref
type Sender = Annotated[TriggerOrigin | IssueOrigin, Field(discriminator="kind")]
# 会话的来源与发起方
type SessionOrigin = Annotated[
    UserOrigin | DelegationOrigin | TriggerOrigin | IssueOrigin, Field(discriminator="kind")
]


class Stored(Protocol):
    """存着来源的会话（表行或读模型）。"""

    @property
    def id(self) -> str: ...
    @property
    def origin(self) -> Origin: ...
    @property
    def origin_ref(self) -> str | None: ...


def origin_of(session: Stored) -> SessionOrigin:
    """会话存下的来源编号的唯一解析：用户、委派没有编号，触发器、issue 的编号是它们的 id。不合格式是存下的数据坏了。"""
    return _parse(session.origin, session.origin_ref, f"session {session.id}")


def _parse(origin: Origin, ref: str | None, where: str) -> SessionOrigin:
    if origin in (Origin.USER, Origin.DELEGATION) and ref is not None:
        raise CorruptState(f"{where}: a {origin} origin has no ref, got {ref!r}")
    match origin:
        case Origin.USER:
            return UserOrigin()
        case Origin.DELEGATION:
            return DelegationOrigin()
        case Origin.TRIGGER:
            return TriggerOrigin(trigger_id=_id(origin, ref, where))
        case Origin.ISSUE:
            return IssueOrigin(issue_id=_id(origin, ref, where))


def _id(origin: Origin, ref: str | None, where: str) -> int:
    if ref is None or not ref.isdecimal():
        raise CorruptState(f"{where}: a {origin} origin needs a numeric ref, got {ref!r}")
    return int(ref)


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
