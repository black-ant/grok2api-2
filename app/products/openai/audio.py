"""OpenAI-compatible Console TTS, STT and Realtime surfaces."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any
from urllib.parse import urlencode

from fastapi import Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from app.control.account.enums import FeedbackKind
from app.control.model import aliases as model_aliases
from app.control.model.enums import ModeId
from app.control.model.spec import ModelSpec
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_console_headers
from app.dataplane.reverse.protocol.xai_console_dpop import build_proof, get_session
from app.dataplane.reverse.transport.console_media import (
    get_tts_voice,
    list_tts_voices,
    synthesize_speech,
    transcribe_speech,
)
from app.dataplane.reverse.transport.websocket import WebSocketClient, WebSocketConnection
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, ValidationError
from app.products.openai.chat import _fail_sync, _feedback_kind, _quota_sync


_OPENAI_VOICES = {
    "alloy": "ara",
    "verse": "ara",
    "echo": "eve",
    "ballad": "eve",
    "fable": "sal",
    "coral": "sal",
    "onyx": "rex",
    "ash": "rex",
    "nova": "leo",
    "sage": "leo",
    "shimmer": "sia",
    "marin": "sia",
}
_AUDIO_FORMATS = {"mp3", "opus", "ogg", "aac", "flac", "wav", "wave", "pcm", "pcm16"}
_STT_FORMATS = {"json", "text", "verbose_json", "srt", "vtt"}


def _resolve_spec(model: str, *, capability: str) -> tuple[str, ModelSpec]:
    resolved = model_aliases.resolve(model)
    if resolved is None:
        raise ValidationError(f"Model {model!r} does not exist", param="model", code="model_not_found")
    spec = resolved.spec
    if capability == "tts" and not spec.is_tts():
        raise ValidationError(f"Model {model!r} is not a TTS model", param="model")
    if capability == "stt" and not spec.is_stt():
        raise ValidationError(f"Model {model!r} is not an STT model", param="model")
    if capability == "realtime" and not spec.is_realtime():
        raise ValidationError(f"Model {model!r} is not a Realtime model", param="model")
    return resolved.model, spec


async def _with_console_account(
    spec: ModelSpec,
    operation: Any,
) -> Any:
    from app.dataplane.account import _directory as directory

    if directory is None:
        raise RateLimitError("Account directory not initialised")
    lease = await directory.reserve(
        pool_candidates=spec.pool_candidates(),
        mode_id=int(ModeId.CONSOLE),
        now_s_override=None,
    )
    if lease is None:
        raise RateLimitError("No available accounts for Console audio")
    token = lease.token
    success = False
    failure: BaseException | None = None
    try:
        result = await operation(token)
        success = True
        return result
    except BaseException as exc:
        failure = exc
        raise
    finally:
        await directory.release(lease)
        kind = FeedbackKind.SUCCESS if success else _feedback_kind(failure) if failure else FeedbackKind.SERVER_ERROR
        await directory.feedback(token, kind, lease.mode_id)
        if success:
            asyncio.create_task(_quota_sync(token, lease.mode_id))
        else:
            asyncio.create_task(_fail_sync(token, lease.mode_id, failure))


def _map_voice(value: str | None) -> str:
    voice = str(value or "").strip()
    return _OPENAI_VOICES.get(voice.lower(), voice)


def _parse_output_format(value: object, response_format: str | None) -> dict[str, Any] | None:
    output: dict[str, Any] = {}
    if isinstance(value, dict):
        output.update(value)
    elif value not in (None, ""):
        raise ValidationError("output_format must be an object", param="output_format")
    codec = str(output.get("codec") or "").strip().lower()
    response_codec = str(response_format or "").strip().lower()
    if not codec:
        codec = response_codec or "mp3"
    if codec == "wave":
        codec = "wav"
    if codec not in _AUDIO_FORMATS:
        raise ValidationError("response_format is not supported", param="response_format")
    output["codec"] = codec
    return output


def _parse_speed(value: object) -> float | None:
    if value is None:
        return None
    try:
        speed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("speed must be a number", param="speed") from exc
    if not 0.25 <= speed <= 4.0:
        raise ValidationError("speed must be between 0.25 and 4", param="speed")
    return speed


async def speech(request: Request) -> Response:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ValidationError("audio speech request must be JSON", param="body") from exc
    if not isinstance(payload, dict):
        raise ValidationError("audio speech request must be an object", param="body")
    model = str(payload.get("model") or "grok-voice-latest").strip()
    real_model, spec = _resolve_spec(model, capability="tts")
    raw_text = payload.get("input") if payload.get("input") is not None else payload.get("text")
    text = str(raw_text or "").strip()
    if not text:
        raise ValidationError("input cannot be empty", param="input")
    if len(text) > 15000:
        raise ValidationError("input must be at most 15000 characters", param="input")
    language = str(payload.get("language") or "en").strip()
    if not language:
        raise ValidationError("language cannot be empty", param="language")
    response_format = str(payload.get("response_format") or "mp3").strip().lower()
    output_format = _parse_output_format(payload.get("output_format"), response_format)
    speed = _parse_speed(payload.get("speed"))
    try:
        optimize = int(payload.get("optimize_streaming_latency")) if payload.get("optimize_streaming_latency") is not None else None
    except (TypeError, ValueError) as exc:
        raise ValidationError("optimize_streaming_latency must be an integer", param="optimize_streaming_latency") from exc
    voice_id = _map_voice(payload.get("voice_id") or payload.get("voice"))
    with_timestamps = bool(payload.get("with_timestamps"))

    async def _run(token: str):
        return await synthesize_speech(
            token,
            text=text,
            language=language,
            voice_id=voice_id,
            output_format=output_format,
            speed=speed,
            optimize_streaming_latency=optimize,
            text_normalization=bool(payload.get("text_normalization")),
            with_timestamps=with_timestamps,
        )

    audio, content_type, envelope = await _with_console_account(spec, _run)
    if with_timestamps and envelope is not None:
        return JSONResponse(envelope)
    return Response(content=audio, media_type=content_type or "audio/mpeg")


async def voices(request: Request, *, voice_id: str | None = None) -> Response:
    model = str(request.query_params.get("model") or "grok-voice-latest").strip()
    _, spec = _resolve_spec(model, capability="tts")

    async def _run(token: str):
        if voice_id is None:
            return await list_tts_voices(token)
        return await get_tts_voice(token, voice_id)

    return JSONResponse(await _with_console_account(spec, _run))


async def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(min(1024 * 1024, max_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValidationError("audio file exceeds the configured size limit", param="file")
        chunks.append(chunk)
    if not chunks:
        raise ValidationError("file cannot be empty", param="file")
    return b"".join(chunks)


async def transcriptions(request: Request) -> Response:
    content_type = request.headers.get("content-type", "").lower()
    model = "grok-stt"
    response_format = "json"
    file_data: bytes | None = None
    file_name = "audio.bin"
    file_mime = "application/octet-stream"
    url = ""
    fields: dict[str, str] = {}
    if content_type.startswith("multipart/"):
        form = await request.form()
        model = str(form.get("model") or model).strip()
        response_format = str(form.get("response_format") or response_format).strip().lower()
        url = str(form.get("url") or "").strip()
        for key in ("audio_format", "sample_rate", "language", "format", "multichannel", "channels", "diarize", "filler_words", "vad_threshold"):
            value = form.get(key)
            if value is not None:
                fields[key] = str(value)
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            file_data = await _read_upload(
                upload,
                max(1, get_config().get_int("voice.input_max_mb", 128)) * 1024 * 1024,
            )
            file_name = getattr(upload, "filename", None) or file_name
            file_mime = getattr(upload, "content_type", None) or file_mime
    elif content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception as exc:
            raise ValidationError("audio transcription request must be JSON", param="body") from exc
        model = str(payload.get("model") or model).strip()
        response_format = str(payload.get("response_format") or response_format).strip().lower()
        url = str(payload.get("url") or "").strip()
        for key in ("audio_format", "sample_rate", "language", "format", "multichannel", "channels", "diarize", "filler_words", "vad_threshold"):
            if payload.get(key) is not None:
                fields[key] = str(payload[key])
    else:
        raise ValidationError("audio/transcriptions requires multipart/form-data or application/json", param="Content-Type")
    if response_format not in _STT_FORMATS:
        raise ValidationError("response_format is not supported", param="response_format")
    _, spec = _resolve_spec(model, capability="stt")

    async def _run(token: str):
        return await transcribe_speech(
            token,
            file_data=file_data,
            file_name=file_name,
            file_content_type=file_mime,
            url=url or None,
            model=model,
            fields=fields,
        )

    result = await _with_console_account(spec, _run)
    if response_format == "text":
        return Response(content=str(result.get("text") or ""), media_type="text/plain")
    if response_format == "srt":
        return Response(content=_to_srt(result), media_type="text/plain")
    if response_format == "vtt":
        return Response(content="WEBVTT\n\n" + _to_srt(result), media_type="text/vtt")
    return JSONResponse(result)


def _to_srt(payload: dict[str, Any]) -> str:
    words = payload.get("words") if isinstance(payload.get("words"), list) else []
    if not words:
        return str(payload.get("text") or "")
    lines: list[str] = []
    for index, item in enumerate(words, 1):
        if not isinstance(item, dict):
            continue
        start = float(item.get("start") or 0)
        end = float(item.get("end") or start)
        text = str(item.get("text") or "").strip()
        if text:
            lines.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
    return "\n".join(lines)


def _timestamp(value: float) -> str:
    millis = max(0, int(round(value * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _api_key_allowed(websocket: WebSocket) -> bool:
    raw = get_config().get("app.api_key", "")
    keys = [str(item).strip() for item in raw] if isinstance(raw, list) else [item.strip() for item in str(raw or "").split(",")]
    keys = [item for item in keys if item]
    if not keys:
        return True
    authorization = websocket.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    supplied = supplied if scheme.lower() == "bearer" else websocket.headers.get("x-api-key", "")
    supplied = supplied or websocket.query_params.get("api_key", "")
    return any(hmac.compare_digest(supplied, key) for key in keys)


async def _connect_voice_websocket(token: str, path: str, model: str) -> tuple[WebSocketConnection, Any]:
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    base_url = get_config().get_str("console.base_url", "https://console.x.ai").rstrip("/")
    http_endpoint = f"{base_url}/v1/{path.lstrip('/')}?{urlencode({'model': model})}"
    endpoint = http_endpoint.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    try:
        session = await get_session(token, lease, base_url=base_url)
        headers = build_console_headers(token, lease=lease)
        headers.pop("Content-Type", None)
        headers["Authorization"] = f"DPoP {session.access_token}"
        headers["DPoP"] = build_proof(session, method="GET", url=http_endpoint)
        headers["Sec-Fetch-Mode"] = "websocket"
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Cache-Control"] = "no-cache"
        connection = await WebSocketClient().connect(
            endpoint,
            headers=headers,
            timeout=get_config().get_float("voice.timeout", 300.0),
            lease=lease,
        )
    except Exception:
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
        raise

    async def _finish() -> None:
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=101))

    return connection, _finish


async def websocket_proxy(websocket: WebSocket, *, path: str) -> None:
    if not _api_key_allowed(websocket):
        await websocket.close(code=4403)
        return
    model = str(websocket.query_params.get("model") or ("grok-stt" if path == "/stt" else "grok-voice-latest"))
    _, spec = _resolve_spec(model, capability="stt" if path == "/stt" else "realtime")
    from app.dataplane.account import _directory as directory

    if directory is None:
        await websocket.close(code=1013)
        return
    account_lease = await directory.reserve(
        pool_candidates=spec.pool_candidates(),
        mode_id=int(ModeId.CONSOLE),
        now_s_override=None,
    )
    if account_lease is None:
        await websocket.close(code=1013)
        return
    upstream: WebSocketConnection | None = None
    proxy_finish = None
    failure: BaseException | None = None
    try:
        upstream, proxy_finish = await _connect_voice_websocket(account_lease.token, path, model)
        await websocket.accept()

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await upstream.ws.send_str(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.ws.send_bytes(message["bytes"])

        async def upstream_to_client() -> None:
            async for message in upstream.ws:
                if message.type.name == "TEXT":
                    await websocket.send_text(message.data)
                elif message.type.name == "BINARY":
                    await websocket.send_bytes(message.data)
                elif message.type.name in {"CLOSE", "CLOSED", "ERROR"}:
                    return

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, (WebSocketDisconnect, asyncio.CancelledError)):
                failure = error
    except (WebSocketDisconnect, asyncio.CancelledError):
        raise
    except BaseException as exc:
        failure = exc
        if not websocket.client_state.name == "DISCONNECTED":
            await websocket.close(code=1011)
    finally:
        if upstream is not None:
            await upstream.close()
        if proxy_finish is not None:
            await proxy_finish()
        await directory.release(account_lease)
        kind = FeedbackKind.SUCCESS if failure is None else _feedback_kind(failure)
        await directory.feedback(account_lease.token, kind, account_lease.mode_id)
        if failure is None:
            asyncio.create_task(_quota_sync(account_lease.token, account_lease.mode_id))
        else:
            asyncio.create_task(_fail_sync(account_lease.token, account_lease.mode_id, failure))


__all__ = ["speech", "transcriptions", "voices", "websocket_proxy"]
