"""向用户提问（ask_user）：问题与回答的模型、回答的校验、给模型与用户的文字。ask_user 是一种挂起的调用，
挂起、唤醒与交回的规则见 sessions.pending；哪些提问现在能回答属于会话领域（conversation.answerable_questions）。
"""

from collections.abc import Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.messages import ToolCallPart

from muliao.errors import InvalidInput

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
# 显式给出并随那一步保存，提问就不再显示为待回答
NOT_ANSWERED = AskResult(
    answered=False,
    answers=[],
    note="The user did not answer these questions and sent a new message instead.",
)
# 同一次运行里问了几次，用户只回答了其中一部分（界面按每次提问分别提交）：没回答的那些如实说明，不能说成发了新消息
SKIPPED = AskResult(
    answered=False,
    answers=[],
    note="The user answered other questions you asked at the same time, but not these.",
)


def questions_of(call: ToolCallPart) -> list[Question]:
    """挂起的 ask_user 调用里的问题：能挂起就说明参数已通过同一个 Questions 类型的校验，读回不合法是数据与代码不一致。"""
    return _QUESTIONS.validate_python(call.args_as_dict()["questions"])


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


def late_answer_lines(results: Sequence[AskResult]) -> list[str]:
    """已经结束的提问的回答，每题一行（sessions.pending.late_text 交给模型）。"""
    lines = []
    for result in results:
        for a in result.answers:
            given = [*a.selected, *([a.other] if a.other else [])]
            lines.append(f"- {a.question}: {'; '.join(given)}")
    return lines


def waiting_text(questions: Sequence[Question]) -> str:
    """运行停在提问上时给用户的通知正文。"""
    return "等你回答：" + "；".join(q.question for q in questions)
