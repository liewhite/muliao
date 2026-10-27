"""服务端配置决定的可选能力，界面据此显示或隐藏对应入口。"""

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C

router = APIRouter()


class FeaturesOut(BaseModel):
    # 配置了 [transcription]：界面显示语音输入
    transcription: bool


@router.get("/features")
async def features(c: C) -> FeaturesOut:
    """服务端配置决定的可选能力，界面据此显示或隐藏对应入口。"""
    return FeaturesOut(transcription=c.transcriber is not None)
