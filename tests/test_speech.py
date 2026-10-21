"""语音输入：OpenAI Chat Completions 音频输入的实现按协议收发与归类错误；接口只在配置了 [transcription] 时可用。"""

import base64
import json

import httpx
import pytest
from pydantic import ValidationError

from muliao.api.app import create_app
from muliao.config import TranscriptionConfig
from muliao.container import build_container
from muliao.errors import InvalidInput, UpstreamFailed
from muliao.platform.db import migrate
from muliao.speech.transcriber import TRANSCRIBE_INSTRUCTIONS, OpenAIChatTranscriber, audio_format
from tests.conftest import login


def _sse(*chunks: object) -> bytes:
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def _delta(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def _transcriber(handler, max_bytes: int = 1000) -> OpenAIChatTranscriber:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIChatTranscriber(
        "sk-test", "https://dash.example/compatible-mode/v1/", "omni", max_bytes, client
    )


async def test_transcribe_streams_audio_as_input_audio_and_joins_the_text_deltas():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        usage = {"choices": [], "usage": {"prompt_tokens": 140, "completion_tokens": 22}}
        return httpx.Response(
            200, content=_sse(_delta(""), _delta("帮我看一下 pull"), _delta(" request 的 CI。\n"), usage)
        )

    text = await _transcriber(handler).transcribe(b"RIFFwav", "audio/webm;codecs=opus")

    assert text == "帮我看一下 pull request 的 CI。"
    assert str(seen[0].url) == "https://dash.example/compatible-mode/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    body = json.loads(seen[0].content)
    assert body["model"] == "omni" and body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": TRANSCRIBE_INSTRUCTIONS}
    [part] = body["messages"][1]["content"]
    assert part == {
        "type": "input_audio",
        "input_audio": {"data": "data:;base64," + base64.b64encode(b"RIFFwav").decode(), "format": "webm"},
    }


async def test_silence_transcribes_to_empty_text():
    text = await _transcriber(lambda _r: httpx.Response(200, content=_sse(_delta("  ")))).transcribe(
        b"x", "audio/mp4"
    )
    assert text == ""


@pytest.mark.parametrize(
    ("media_type", "fmt"),
    [("audio/webm;codecs=opus", "webm"), ("audio/mp4", "mp4"), ("Audio/WAV", "wav"), ("audio/mpeg", "mp3")],
)
def test_browser_recording_types_map_to_audio_formats(media_type, fmt):
    assert audio_format(media_type) == fmt


async def test_non_audio_type_and_empty_audio_are_rejected_before_calling_the_service():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("service must not be called")

    with pytest.raises(InvalidInput, match="unsupported audio type"):
        await _transcriber(handler).transcribe(b"x", "image/png")
    with pytest.raises(InvalidInput, match="empty"):
        await _transcriber(handler).transcribe(b"", "audio/webm")


async def test_service_errors_surface_as_upstream_failed_with_the_reason():
    def http_error(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Incorrect API key provided"}})

    with pytest.raises(UpstreamFailed, match="HTTP 401.*Incorrect API key"):
        await _transcriber(http_error).transcribe(b"x", "audio/webm")

    def stream_error(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"error": {"message": "audio too long"}}))

    with pytest.raises(UpstreamFailed, match="audio too long"):
        await _transcriber(stream_error).transcribe(b"x", "audio/webm")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamFailed, match="ConnectError"):
        await _transcriber(unreachable).transcribe(b"x", "audio/webm")


def test_config_requires_model_and_base_url():
    with pytest.raises(ValidationError):
        TranscriptionConfig.model_validate({"type": "openai", "api_key": "k", "model": "m"})
    cfg = TranscriptionConfig.model_validate(
        {"type": "openai", "api_key": "k", "model": "m", "base_url": "https://x"}
    )
    assert cfg.max_bytes > 0


async def test_without_config_features_say_no_transcription_and_the_endpoint_is_404(client):
    await login(client)
    assert (await client.get("/api/features")).json() == {"transcription": False}
    resp = await client.post("/api/transcriptions", content=b"x", headers={"content-type": "audio/webm"})
    assert resp.status_code == 404


@pytest.fixture
async def speech_client(settings, models, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_delta("deploy 到 production")))

    c = build_container(
        settings,
        resolve_model=models,
        model_checker=lambda _m: None,
        transcriber=_transcriber(handler, max_bytes=10),
    )
    migrate(settings.db_path)
    transport = httpx.ASGITransport(app=create_app(c, tmp_path / "no-web"))
    async with c.jobs.running(), httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    await c.db.engine.dispose()


async def test_configured_transcription_turns_posted_audio_into_text(speech_client):
    await login(speech_client)
    assert (await speech_client.get("/api/features")).json() == {"transcription": True}

    resp = await speech_client.post(
        "/api/transcriptions", content=b"opus", headers={"content-type": "audio/webm"}
    )
    assert resp.status_code == 200 and resp.json() == {"text": "deploy 到 production"}

    too_big = await speech_client.post(
        "/api/transcriptions", content=b"x" * 11, headers={"content-type": "audio/webm"}
    )
    assert too_big.status_code == 413
    no_type = await speech_client.post("/api/transcriptions", content=b"x")
    assert no_type.status_code == 422


async def test_transcription_requires_login(speech_client):
    resp = await speech_client.post(
        "/api/transcriptions", content=b"x", headers={"content-type": "audio/webm"}
    )
    assert resp.status_code == 401
