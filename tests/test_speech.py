"""语音输入：百炼语音识别的实现按协议收发与归类错误；接口只在配置了 [transcription] 时可用。"""

import base64
import json

import httpx
import pytest
from pydantic import ValidationError

from muliao.api.app import create_app
from muliao.config import TranscriptionConfig
from muliao.container import assemble
from muliao.errors import InvalidInput, UpstreamFailed
from muliao.platform.db import migrate
from muliao.speech.transcriber import DashScopeTranscriber, audio_type
from tests.conftest import login


def _transcriber(handler, max_bytes: int = 1000, **kw) -> DashScopeTranscriber:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return DashScopeTranscriber(
        "sk-test", "asr", max_bytes, base_url="https://dash.example/api/v1/", client=client, **kw
    )


def _result(text: str) -> httpx.Response:
    return httpx.Response(200, json={"output": {"text": text}, "usage": {"duration": 3}, "request_id": "r"})


async def test_transcribe_posts_the_audio_inline_with_its_format_and_returns_the_text():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _result("帮我看一下 pull request 的 CI。 ")

    t = _transcriber(handler, vocabulary={"Grafana": 5}, language_hints=["zh", "en"])
    text = await t.transcribe(b"opus", "audio/webm;codecs=opus")

    assert text == "帮我看一下 pull request 的 CI。"
    assert str(seen[0].url) == "https://dash.example/api/v1/services/aigc/multimodal-generation/generation"
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    body = json.loads(seen[0].content)
    assert body["model"] == "asr"
    assert body["parameters"] == {
        "format": "webm",
        "vocabulary": {"Grafana": 5},
        "language_hints": ["zh", "en"],
    }
    [message] = body["input"]["messages"]
    assert message["content"] == [
        {
            "type": "input_audio",
            "input_audio": {"data": "data:audio/webm;base64," + base64.b64encode(b"opus").decode()},
        }
    ]


async def test_optional_parameters_are_left_out_when_not_configured():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _result("hi")

    await _transcriber(handler).transcribe(b"x", "audio/mp4")
    assert seen[0]["parameters"] == {"format": "mp4"}


async def test_silence_transcribes_to_empty_text():
    assert await _transcriber(lambda _r: _result("  ")).transcribe(b"x", "audio/mp4") == ""
    no_text = httpx.Response(200, json={"output": {}, "request_id": "r"})
    assert await _transcriber(lambda _r: no_text).transcribe(b"x", "audio/mp4") == ""


@pytest.mark.parametrize(
    ("media_type", "fmt"),
    [("audio/webm;codecs=opus", "webm"), ("audio/mp4", "mp4"), ("Audio/WAV", "wav"), ("audio/mpeg", "mp3")],
)
def test_browser_recording_types_map_to_audio_formats(media_type, fmt):
    assert audio_type(media_type)[1] == fmt


async def test_non_audio_type_and_empty_audio_are_rejected_before_calling_the_service():
    def handler(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("service must not be called")

    with pytest.raises(InvalidInput, match="unsupported audio type"):
        await _transcriber(handler).transcribe(b"x", "image/png")
    with pytest.raises(InvalidInput, match="empty"):
        await _transcriber(handler).transcribe(b"", "audio/webm")


async def test_service_errors_surface_as_upstream_failed_with_the_reason():
    def http_error(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": "UNSUPPORTED_FORMAT", "message": "format is empty"})

    with pytest.raises(UpstreamFailed, match="HTTP 400.*format is empty"):
        await _transcriber(http_error).transcribe(b"x", "audio/webm")

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(UpstreamFailed, match="ConnectError"):
        await _transcriber(unreachable).transcribe(b"x", "audio/webm")


def test_config_requires_type_model_and_key():
    with pytest.raises(ValidationError):
        TranscriptionConfig.model_validate({"type": "dashscope", "api_key": "k"})
    with pytest.raises(ValidationError):
        TranscriptionConfig.model_validate({"type": "openai", "api_key": "k", "model": "m"})
    cfg = TranscriptionConfig.model_validate({"type": "dashscope", "api_key": "k", "model": "m"})
    assert cfg.max_bytes > 0 and cfg.base_url is None and cfg.vocabulary == {}


async def test_without_config_features_say_no_transcription_and_the_endpoint_is_404(client):
    await login(client)
    assert (await client.get("/api/features")).json() == {"transcription": False}
    resp = await client.post("/api/transcriptions", content=b"x", headers={"content-type": "audio/webm"})
    assert resp.status_code == 404


@pytest.fixture
async def speech_client(settings, models, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return _result("deploy 到 production")

    c = assemble(
        settings,
        resolve_model=models,
        model_checker=lambda _m: None,
        transcriber=_transcriber(handler, max_bytes=10),
    )
    migrate(settings.db_path)
    transport = httpx.ASGITransport(app=create_app(c.container, tmp_path / "no-web"))
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
