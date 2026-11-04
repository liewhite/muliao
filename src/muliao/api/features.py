"""服务端配置决定的界面行为：可选能力（显示或隐藏对应入口）与界面要展示的默认值。"""

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.sessions.api import MIN_COMPACTION_THRESHOLD_TOKENS

router = APIRouter()


class FeaturesOut(BaseModel):
    # 配置了 [transcription]：界面显示语音输入
    transcription: bool
    # 全局默认压缩阈值（config [runtime]）：新建会话时不填就用它
    compaction_threshold_tokens: int
    # 新建会话时自定义压缩阈值的下限
    min_compaction_threshold_tokens: int


@router.get("/features")
async def features(c: C) -> FeaturesOut:
    """服务端配置决定的界面行为：可选能力与界面要展示的默认值。"""
    return FeaturesOut(
        transcription=c.transcriber is not None,
        compaction_threshold_tokens=c.sessions.default_compaction_threshold,
        min_compaction_threshold_tokens=MIN_COMPACTION_THRESHOLD_TOKENS,
    )
