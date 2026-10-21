"""语音转文字：界面录下的一段语音转成文字，由界面作为用户消息发给 agent（agent 只看到文字）。

`Transcriber` 是唯一接口，具体服务是它的实现，由配置 `[transcription]` 的 type 选择。用户说话时中英文混杂，
用专门的语音识别模型（不是全模态对话模型：后者会把语音当成提问来回答，还要先思考，慢）。

转写是用户在界面上同步等待的一次调用，不进任务队列：服务出错或不可达时报 UpstreamFailed，原因原样交给界面，
由用户决定重录。
"""

import base64
import json
import logging
import time
from typing import Protocol, assert_never

import httpx

from muliao.config import TranscriptionConfig
from muliao.errors import InvalidInput, UpstreamFailed

log = logging.getLogger(__name__)

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"

# 浏览器录音的 MIME 类型（去掉 ;codecs= 参数后）→ 服务的音频格式参数。
# Chrome/Firefox 录成 webm 或 ogg（Opus），iOS Safari 录成 mp4（AAC）
AUDIO_FORMATS: dict[str, str] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/flac": "flac",
    "audio/amr": "amr",
}

# 一段语音转写通常一两秒；服务挂起时让用户尽早看到失败，而不是一直转圈
_TIMEOUT_SECONDS = 60.0


def audio_type(media_type: str) -> tuple[str, str]:
    """请求的 Content-Type → (不带参数的 MIME 类型, 音频格式)；不是支持的音频类型时报 InvalidInput。"""
    base = media_type.split(";", 1)[0].strip().lower()
    if base not in AUDIO_FORMATS:
        raise InvalidInput(f"unsupported audio type {media_type!r}; supported: {sorted(AUDIO_FORMATS)}")
    return base, AUDIO_FORMATS[base]


class Transcriber(Protocol):
    # 一段语音的字节上限，HTTP 层按它读取请求体
    max_bytes: int

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        """转写出的文字（首尾空白已去掉）；听不到人说话时为空字符串。"""
        ...


class DashScopeTranscriber:
    """百炼录音文件识别的同步接口（multimodal-generation，非流式）：音频以 data URI 内联，一次返回整段文字。"""

    def __init__(
        self,
        api_key: str,
        model: str,
        max_bytes: int,
        *,
        base_url: str = DASHSCOPE_BASE_URL,
        vocabulary: dict[str, int] | None = None,
        language_hints: list[str] | None = None,
        client: httpx.AsyncClient,
    ):
        # 共用的出站 HTTP 客户端，由容器创建、在关停时关闭
        self._client = client
        self._url = base_url.rstrip("/") + "/services/aigc/multimodal-generation/generation"
        self._headers = {"Authorization": f"Bearer {api_key}", "X-DashScope-SSE": "disable"}
        self._model = model
        self._vocabulary = dict(vocabulary or {})
        self._language_hints = list(language_hints or [])
        self.max_bytes = max_bytes

    def _body(self, audio: bytes, mime: str, fmt: str) -> dict[str, object]:
        parameters: dict[str, object] = {"format": fmt}
        if self._vocabulary:
            parameters["vocabulary"] = self._vocabulary
        if self._language_hints:
            parameters["language_hints"] = self._language_hints
        data = base64.b64encode(audio).decode()
        audio_part = {"type": "input_audio", "input_audio": {"data": f"data:{mime};base64,{data}"}}
        return {
            "model": self._model,
            "input": {"messages": [{"role": "user", "content": [audio_part]}]},
            "parameters": parameters,
        }

    async def transcribe(self, audio: bytes, media_type: str) -> str:
        mime, fmt = audio_type(media_type)
        if not audio:
            raise InvalidInput("audio is empty")
        started = time.monotonic()
        try:
            resp = await self._client.post(
                self._url,
                json=self._body(audio, mime, fmt),
                headers=self._headers,
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as e:
            raise self._failed(f"{type(e).__name__}: {e}", fmt, len(audio)) from e
        if resp.status_code != 200:
            raise self._failed(f"HTTP {resp.status_code}: {resp.text[:500]}", fmt, len(audio))
        try:
            body = resp.json()
        except json.JSONDecodeError as e:
            raise self._failed(f"non-JSON response: {resp.text[:200]}", fmt, len(audio)) from e
        text = ((body.get("output") or {}).get("text") or "").strip()
        log.info(
            "audio transcribed",
            extra={
                "model": self._model,
                "format": fmt,
                "bytes": len(audio),
                "chars": len(text),
                "seconds": round(time.monotonic() - started, 2),
                "usage": body.get("usage"),
            },
        )
        return text

    def _failed(self, reason: str, fmt: str, size: int) -> UpstreamFailed:
        log.warning(
            "transcription failed",
            extra={"model": self._model, "format": fmt, "bytes": size, "reason": reason},
        )
        return UpstreamFailed(f"transcription failed ({self._model}): {reason}")


def build_transcriber(cfg: TranscriptionConfig, client: httpx.AsyncClient) -> Transcriber:
    match cfg.type:
        case "dashscope":
            return DashScopeTranscriber(
                cfg.api_key.get_secret_value(),
                cfg.model,
                cfg.max_bytes,
                base_url=cfg.base_url or DASHSCOPE_BASE_URL,
                vocabulary=cfg.vocabulary,
                language_hints=cfg.language_hints,
                client=client,
            )
        case _:
            assert_never(cfg.type)
