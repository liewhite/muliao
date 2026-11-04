"""投递目标：一个事件交给谁（纯逻辑）。

目标有三种，存的是 id（agent 改名不影响已存的目标），对模型与脚本用文字地址（按名称指代 agent）：
- 新会话 `new` / `agent:<名>`：新开一个会话（默认是发送方自己的 agent）；
- 最近的会话 `session` / `session:<名>`：续写这个发送方最近一次给该 agent 开的会话（会话领域按发送方定位，SessionKey），
  没有就新建；长度靠会话的压缩；
- issue `issue:<N>`：记进 issue 的时间线，由 issue 领域按它的规则投给接手的 agent（没人接手时给 owner）。

事件文本里可以按段指定目标（split_routed）：单独一行 `@route <地址>`，从它到下一个 `@route` 之间的内容投到这个地址；
第一个 `@route` 之前的内容投到默认目标。渠道（Slack、Telegram……）的逻辑都在产出事件的脚本里，这里只认地址。
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from muliao.agents.api import AgentNames
from muliao.errors import InvalidInput


class NewTo(BaseModel):
    """新开一个会话。agent_id 为空时是发送方自己的 agent。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["new"] = "new"
    agent_id: str | None = None


class LatestOf(BaseModel):
    """续写这个发送方最近一次给该 agent 开的会话，没有就新建。agent_id 为空时是发送方自己的 agent。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["session"] = "session"
    agent_id: str | None = None


class IssueTo(BaseModel):
    """记进 issue 的时间线，按 issue 的规则投递。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    to: Literal["issue"] = "issue"
    issue_id: int


Target = Annotated[NewTo | LatestOf | IssueTo, Field(discriminator="to")]
TARGET = TypeAdapter[NewTo | LatestOf | IssueTo](Target)

# 默认目标：新开发送方自己的 agent 的会话
DEFAULT: NewTo = NewTo()

# 地址的格式（给模型与界面看的说明）
ADDRESS_HELP = (
    "`new` (a new conversation of the sender's own agent, the default), `agent:<name>` (a new conversation of "
    "that agent), `session` or `session:<name>` (continue the latest conversation this sender started with its "
    "own agent or that agent; a new one if none yet), `issue:<N>` (recorded on issue #N's timeline and delivered "
    "like a user comment: to the agent working on it, otherwise to its owner)"
)


class Address(BaseModel):
    """文字地址解析出的目标，agent 还是名称（解析成 id 要查库，见 routing.router.resolve_in）。"""

    model_config = ConfigDict(frozen=True)
    to: Literal["new", "session", "issue"]
    agent: str | None = None
    issue_id: int | None = None


_ADDRESS = re.compile(r"^(?:(new)|(agent):(\S+)|(session)(?::(\S+))?|(issue):#?(\d+))$")


def parse_address(text: str) -> Address:
    """`new` / `agent:<名>` / `session[:<名>]` / `issue:<N>`，不合法时 InvalidInput。"""
    m = _ADDRESS.fullmatch(text.strip())
    if m is None:
        raise InvalidInput(f"invalid route {text!r}; use one of: {ADDRESS_HELP}")
    new, agent, agent_name, session, session_agent, issue, issue_id = m.groups()
    if new:
        return Address(to="new")
    if agent:
        return Address(to="new", agent=agent_name)
    if session:
        return Address(to="session", agent=session_agent)
    assert issue
    return Address(to="issue", issue_id=int(issue_id))


_ROUTE_LINE = re.compile(r"^@route[ \t]+(\S+)[ \t]*$")


def split_routed(text: str) -> list[tuple[str | None, str]]:
    """把事件文本按 `@route <地址>` 行（必须顶格）分段：[(地址或 None（默认目标）, 内容)]，按出现顺序，没有内容的段丢掉
    （同一目标的合并在解析成目标之后，见 router.plan_in）。没有 `@route` 行时原样是一段（不改动现有的事件）。"""
    lines = text.splitlines()
    if not any(_ROUTE_LINE.match(line) for line in lines):
        return [(None, text)]
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in lines:
        if m := _ROUTE_LINE.match(line):
            sections.append((m.group(1), []))
        else:
            sections[-1][1].append(line)
    return [(address, "\n".join(body).strip("\n")) for address, body in sections if "".join(body).strip()]


def describe(target: NewTo | LatestOf | IssueTo, names: AgentNames) -> str:
    """目标的文字地址（parse_address 的逆）：给模型与界面看。发送方自己的 agent 存为 None，不写名字。"""
    match target:
        case NewTo(agent_id=None):
            return "new"
        case NewTo(agent_id=agent_id):
            return f"agent:{names[agent_id]}"
        case LatestOf(agent_id=None):
            return "session"
        case LatestOf(agent_id=agent_id):
            return f"session:{names[agent_id]}"
        case IssueTo(issue_id=issue_id):
            return f"issue:{issue_id}"
