"""语音输入：界面录音后把原始音频发到这里，拿回文字再作为消息发出（同 /api/files：请求体就是内容，Content-Type 是类型）。"""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from muliao.api.deps import C, read_limited
from muliao.errors import InvalidInput, NotFound

router = APIRouter()


class TranscriptOut(BaseModel):
    # 听不到人说话时为空字符串
    text: str


@router.post("/transcriptions")
async def transcribe(c: C, request: Request) -> TranscriptOut:
    """请求体是一段语音（webm、ogg、mp4、wav、mp3……），返回转写出的文字；转写服务出错时 502 并带上原因。"""
    if c.transcriber is None:
        raise NotFound("speech transcription is not configured ([transcription] in config.toml)")
    media_type = request.headers.get("content-type")
    if not media_type:
        raise InvalidInput("Content-Type header is required: it is the type of the audio")
    audio = await read_limited(request, c.transcriber.max_bytes)
    return TranscriptOut(text=await c.transcriber.transcribe(audio, media_type))
