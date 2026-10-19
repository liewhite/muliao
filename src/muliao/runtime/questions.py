"""向用户提问（ask_user）：问题与回答的模型、哪些提问还在等回答、什么时候能回答、回答的校验。

一轮里模型调用 ask_user 时，工具抛出 pydantic-ai 的 CallDeferred：这一轮正常结束并提交（调用暂时没有结果），
不在 worker 里等人。用户在界面上提交回答 = 新的一轮，回答作为这次调用的结果（DeferredToolResults）交回模型，
所以模型看到的就是「我问了，用户这样答」。用户不答、直接发消息时，那一轮把挂起的提问显式记为没有回答
（deferred_results），结果随它持久化。所以挂起的提问只可能出现在会话最后一轮里。
"""

from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.messages import ModelMessage, ToolCallPart
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import Conflict, InvalidInput
from muliao.models import Message, Task, TaskKind, TaskStatus
from muliao.runtime.codec import load_messages
from muliao.transcript import unanswered_calls

ASK_USER = "ask_user"


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80, description="Short choice text shown on the button.")
    description: str = Field(
        default="", max_length=300, description="What choosing it means, if not obvious."
    )


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500, description="The full question.")
    header: str = Field(min_length=1, max_length=20, description="Very short label, e.g. 'Timeframe'.")
    options: list[Option] = Field(min_length=2, max_length=6)
    multi_select: bool = Field(default=False, description="Allow choosing several options.")


QUESTIONS_MAX = 4
# ask_user 的参数与读回调用时用同一个类型：题数约束只写这一处
Questions = Annotated[list[Question], Field(min_length=1, max_length=QUESTIONS_MAX)]
_QUESTIONS: TypeAdapter[list[Question]] = TypeAdapter(Questions)


class QuestionAnswer(BaseModel):
    """用户对一个问题的回答：选中的选项，和/或「其他」里填的文字。"""

    model_config = ConfigDict(extra="forbid")

    selected: list[str] = []
    other: str | None = None


class AnsweredQuestion(BaseModel):
    question: str
    selected: list[str]
    other: str | None


class AskResult(BaseModel):
    """一次提问交给模型的结果（也是界面展示已回答提问的来源）：answered 区分用户回答了，还是没回答就发了新消息。"""

    answered: bool
    answers: list[AnsweredQuestion]
    note: str | None


# 用户没回答、直接发了新消息：挂起的提问必须有个结果才能继续（pydantic-ai 拒绝在未处理的调用后追加新消息），
# 显式给出并随这一轮持久化，提问就不再显示为待回答
NOT_ANSWERED = AskResult(
    answered=False,
    answers=[],
    note="The user did not answer these questions and sent a new message instead.",
)


def pending_questions(messages: Sequence[ModelMessage]) -> dict[str, list[Question]]:
    """还在等回答的提问：没有结果的 ask_user 调用（参数被 schema 拒绝的调用有重试结果，不算）。按出现顺序。"""
    return {c.tool_call_id: questions_of(c) for c in unanswered_calls(messages) if c.tool_name == ASK_USER}


def questions_of(call: ToolCallPart) -> list[Question]:
    """挂起的 ask_user 调用里的问题：能挂起就说明参数已通过同一个 Questions 类型的校验，读回不合法是数据与代码不一致。"""
    return _QUESTIONS.validate_python(call.args_as_dict()["questions"])


async def pending_in_session(s: AsyncSession, session_id: str) -> dict[str, list[Question]]:
    """每一轮都会关闭之前挂起的提问，所以只需要看最后一轮的消息。"""
    last_turn = select(func.max(Message.task_id)).where(Message.session_id == session_id).scalar_subquery()
    rows = await s.scalars(
        select(Message.payload)
        .where(Message.session_id == session_id, Message.task_id == last_turn)
        .order_by(Message.id)
    )
    return pending_questions(load_messages(rows.all()))


def answering_blocked(open_turns: Sequence[Task]) -> bool:
    """回答必须紧接在提问那一轮后面执行。排队或运行中的轮次会先跑完并把提问记为没回答；失败待重试的回答
    还占着这次提问。这两种情况下不接受回答（界面也不显示表单），失败的回答只能重试。"""
    return any(
        t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
        or (t.status == TaskStatus.FAILED and t.payload["answers"])
        for t in open_turns
    )


async def answerable_questions(s: AsyncSession, session_id: str) -> dict[str, list[Question]] | None:
    """这个会话现在能回答的提问；有会先执行的轮次或失败待重试的回答时为 None（见 answering_blocked）。
    界面展示与 receive 校验都经过这里。"""
    if answering_blocked(await _open_turns(s, session_id)):
        return None
    return await pending_in_session(s, session_id)


async def _open_turns(s: AsyncSession, session_id: str) -> list[Task]:
    rows = await s.scalars(
        select(Task).where(
            Task.session_id == session_id, Task.kind == TaskKind.TURN, Task.status != TaskStatus.DONE
        )
    )
    return list(rows.all())


def answer_result(questions: Sequence[Question], answers: Sequence[QuestionAnswer]) -> AskResult:
    """校验回答并整理成交给模型的工具结果。回答来自用户，不合法时报 InvalidInput。"""
    if len(answers) != len(questions):
        raise InvalidInput(f"expected {len(questions)} answers, got {len(answers)}")
    result: list[AnsweredQuestion] = []
    for i, (q, a) in enumerate(zip(questions, answers, strict=True), start=1):
        labels = {o.label for o in q.options}
        if unknown := [s for s in a.selected if s not in labels]:
            raise InvalidInput(f"question {i}: {unknown} are not options of {q.question!r}")
        if len(set(a.selected)) != len(a.selected):
            raise InvalidInput(f"question {i}: an option is selected twice")
        if not q.multi_select and len(a.selected) > 1:
            raise InvalidInput(f"question {i} allows one choice, got {len(a.selected)}")
        other = a.other.strip() if a.other is not None else ""
        if not a.selected and not other:
            raise InvalidInput(f"question {i} is not answered")
        result.append(AnsweredQuestion(question=q.question, selected=a.selected, other=other or None))
    return AskResult(answered=True, answers=result, note=None)


async def resolve_answers(
    s: AsyncSession, session_id: str, answers: Mapping[str, Sequence[QuestionAnswer]]
) -> dict[str, dict[str, object]]:
    """用户提交的回答：会话里没有会先执行的轮次，且每个都对应还在等回答的提问。返回交给模型的结果（payload 用）。"""
    pending = await answerable_questions(s, session_id)
    if pending is None:
        raise Conflict(
            f"session {session_id} has a turn that runs first (or a failed answer to retry); "
            "it would close these questions, so they can no longer be answered"
        )
    resolved = {}
    for call_id, given in answers.items():
        if call_id not in pending:
            raise Conflict(f"question {call_id!r} is not waiting for an answer in session {session_id}")
        resolved[call_id] = answer_result(pending[call_id], given).model_dump()
    return resolved


def deferred_results(pending: Mapping[str, object], answers: Mapping[str, object]) -> dict[str, object]:
    """这一轮交给 pydantic-ai 的挂起调用结果：有回答的用回答，其余挂起的提问一律记为没有回答。"""
    return {call_id: answers.get(call_id, NOT_ANSWERED.model_dump()) for call_id in pending}


def waiting_text(questions: Sequence[Question]) -> str:
    """一轮停在提问上时给用户的通知正文。"""
    return "等你回答：" + "；".join(q.question for q in questions)
