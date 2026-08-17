"""Console image, video and audio transports."""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlparse

import orjson

from app.dataplane.reverse.protocol.xai_console_dpop import request_bytes
from app.dataplane.reverse.protocol.xai_console_media import (
    build_image_edit_payload,
    build_image_generation_payload,
    build_video_generation_payload,
    parse_video_create,
    parse_video_status,
    safe_error_text,
)
from app.dataplane.reverse.transport.assets import download_asset
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.platform.storage import save_local_image, save_local_video


_IMAGE_HOSTS = {"assets.grok.com", "imagine-public.x.ai", "imgen.x.ai"}
_VIDEO_HOST_SUFFIX = ".vidgen.x.ai"


def _console_url(path: str) -> str:
    base = get_config().get_str("console.base_url", "https://console.x.ai").rstrip("/")
    return f"{base}/v1/{path.lstrip('/')}"


def _local_url(kind: str, file_id: str) -> str:
    base = get_config().get_str("app.app_url", "").rstrip("/")
    path = f"/v1/files/{kind}?id={file_id}"
    return f"{base}{path}" if base else path


def _trusted_image_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and (parsed.hostname or "").lower() in _IMAGE_HOSTS
    )


def _trusted_video_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme.lower() == "https"
        and parsed.username is None
        and parsed.password is None
        and (
            host == "grok.com"
            or host.endswith(".grok.com")
            or host == "vidgen.x.ai"
            or host.endswith(_VIDEO_HOST_SUFFIX)
        )
    )


async def _download_asset_bytes(token: str, url: str, *, max_bytes: int, expected: str) -> tuple[bytes, str]:
    validator = _trusted_image_url if expected == "image" else _trusted_video_url
    stream, content_type = await download_asset(token, url, url_validator=validator)
    chunks: list[bytes] = []
    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > max_bytes:
            raise UpstreamError(f"Console {expected} output exceeds the safety limit", status=502)
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise UpstreamError(f"Console {expected} output is empty", status=502)
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    return raw, normalized


async def localize_image_response(token: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, list) or not data or len(data) > 10:
        raise UpstreamError("Console image response has no valid data", status=502)
    localized: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise UpstreamError("Console image response item is invalid", status=502)
        raw_url = str(item.get("url") or "").strip()
        if raw_url and _trusted_image_url(raw_url):
            raw, mime = await _download_asset_bytes(
                token,
                raw_url,
                max_bytes=32 * 1024 * 1024,
                expected="image",
            )
            file_id = hashlib.sha1(raw).hexdigest()[:32]
            await asyncio.to_thread(save_local_image, raw, mime or "image/jpeg", file_id)
            localized.append(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"url", "b64_json"}
                }
                | {"url": _local_url("image", file_id), "mime_type": mime or "image/jpeg"}
            )
            continue
        encoded = str(item.get("b64_json") or "").strip()
        if encoded:
            localized.append({"b64_json": encoded})
            continue
        raise UpstreamError("Console image response item has no trusted URL", status=502)
    return {**payload, "data": localized}


async def generate_image(
    token: str,
    *,
    model: str,
    prompt: str,
    count: int,
    response_format: str,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    payload = build_image_generation_payload(
        model=model,
        prompt=prompt,
        count=count,
        response_format=response_format,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        quality=quality,
    )
    _, _, body = await request_bytes(
        token,
        "POST",
        _console_url("images/generations"),
        body=orjson.dumps(payload),
        timeout_s=get_config().get_float("image.timeout", 180.0),
    )
    try:
        result = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise UpstreamError("Console image response is not valid JSON", status=502) from exc
    if response_format.strip().lower() == "url":
        return await localize_image_response(token, result)
    return result


async def edit_image(
    token: str,
    *,
    model: str,
    prompt: str,
    image_urls: list[str],
    count: int,
    response_format: str,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    payload = build_image_edit_payload(
        model=model,
        prompt=prompt,
        image_urls=image_urls,
        count=count,
        response_format=response_format,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        quality=quality,
    )
    _, _, body = await request_bytes(
        token,
        "POST",
        _console_url("images/edits"),
        body=orjson.dumps(payload),
        timeout_s=get_config().get_float("image.timeout", 180.0),
    )
    try:
        result = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise UpstreamError("Console image edit response is not valid JSON", status=502) from exc
    if response_format.strip().lower() == "url":
        return await localize_image_response(token, result)
    return result


async def generate_video(
    token: str,
    *,
    model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    image_url: str | None = None,
    reference_urls: list[str] | None = None,
    reference_audios: list[str] | None = None,
    progress: Callable[[int], Any] | None = None,
) -> tuple[str, str]:
    payload = build_video_generation_payload(
        model=model,
        prompt=prompt,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        image_url=image_url,
        reference_urls=reference_urls,
        reference_audios=reference_audios,
    )
    _, _, body = await request_bytes(
        token,
        "POST",
        _console_url("videos/generations"),
        body=orjson.dumps(payload),
        timeout_s=get_config().get_float("video.timeout", 180.0),
    )
    try:
        request_id = parse_video_create(body)
    except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
        raise UpstreamError("Console video create response is invalid", status=502) from exc
    if progress is not None:
        await progress(1)

    timeout_s = get_config().get_float("video.timeout", 180.0)
    deadline = asyncio.get_running_loop().time() + max(30.0, timeout_s)
    while True:
        if asyncio.get_running_loop().time() >= deadline:
            raise UpstreamError("Console video generation timed out", status=504)
        _, _, status_body = await request_bytes(
            token,
            "GET",
            _console_url(f"videos/{request_id}"),
            timeout_s=min(60.0, timeout_s),
        )
        try:
            status, percent, video_url, error_text = parse_video_status(status_body)
        except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
            raise UpstreamError("Console video status response is invalid", status=502) from exc
        if progress is not None and percent > 0:
            await progress(min(99, percent))
        if status in {"done", "completed", "succeeded", "success", "ready"}:
            if not video_url or not _trusted_video_url(video_url):
                raise UpstreamError("Console video completed without a trusted URL", status=502)
            if progress is not None:
                await progress(100)
            return video_url, "video/mp4"
        if status in {"failed", "expired", "cancelled", "canceled", "error"}:
            raise UpstreamError(
                f"Console video generation failed: {safe_error_text(error_text or status)}",
                status=502,
            )
        if status not in {"pending", "processing", "in_progress", "queued"}:
            raise UpstreamError(f"Console video status is invalid: {status}", status=502)
        await asyncio.sleep(2.0)


async def localize_video(token: str, url: str) -> str:
    if not _trusted_video_url(url):
        raise UpstreamError("Console video URL is not trusted", status=502)
    raw, mime = await _download_asset_bytes(
        token,
        url,
        max_bytes=512 * 1024 * 1024,
        expected="video",
    )
    file_id = hashlib.sha1(raw).hexdigest()[:32]
    path = await asyncio.to_thread(save_local_video, raw, file_id, mime or "video/mp4")
    return _local_url("video", file_id) if path.exists() else _local_url("video", file_id)


async def synthesize_speech(
    token: str,
    *,
    text: str,
    language: str,
    voice_id: str = "",
    output_format: dict[str, Any] | None = None,
    speed: float | None = None,
    optimize_streaming_latency: int | None = None,
    text_normalization: bool = False,
    with_timestamps: bool = False,
) -> tuple[bytes, str, dict[str, Any] | None]:
    payload: dict[str, Any] = {"text": text, "language": language}
    if voice_id.strip():
        payload["voice_id"] = voice_id.strip()
    if output_format:
        payload["output_format"] = output_format
    if speed is not None:
        payload["speed"] = speed
    if optimize_streaming_latency is not None:
        payload["optimize_streaming_latency"] = str(optimize_streaming_latency)
    if text_normalization:
        payload["text_normalization"] = True
    if with_timestamps:
        payload["with_timestamps"] = True
    _, headers, body = await request_bytes(
        token,
        "POST",
        _console_url("tts"),
        body=orjson.dumps(payload),
        accept="*/*",
        timeout_s=get_config().get_float("voice.timeout", 180.0),
    )
    content_type = headers.get("Content-Type", headers.get("content-type", "audio/mpeg"))
    content_type = content_type.split(";", 1)[0].strip().lower() or "audio/mpeg"
    envelope: dict[str, Any] | None = None
    if "json" in content_type or with_timestamps:
        try:
            envelope = orjson.loads(body)
            encoded = str(envelope.get("audio") or "").strip()
            audio = base64.b64decode(encoded, validate=True)
            content_type = str(envelope.get("content_type") or content_type).split(";", 1)[0]
            return audio, content_type, envelope
        except (ValueError, TypeError, orjson.JSONDecodeError) as exc:
            if "json" in content_type:
                raise UpstreamError("Console TTS response is invalid", status=502) from exc
    return body, content_type, envelope


async def list_tts_voices(token: str) -> dict[str, Any]:
    _, _, body = await request_bytes(
        token,
        "GET",
        _console_url("tts/voices"),
        accept="application/json",
        timeout_s=get_config().get_float("voice.timeout", 120.0),
    )
    try:
        result = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise UpstreamError("Console TTS voice list is invalid JSON", status=502) from exc
    if not isinstance(result, dict):
        raise UpstreamError("Console TTS voice list is invalid", status=502)
    return result


async def get_tts_voice(token: str, voice_id: str) -> dict[str, Any]:
    normalized = voice_id.strip()
    if not normalized:
        raise ValueError("voice_id cannot be empty")
    _, _, body = await request_bytes(
        token,
        "GET",
        _console_url(f"tts/voices/{quote(normalized, safe='')}"),
        accept="application/json",
        timeout_s=get_config().get_float("voice.timeout", 120.0),
    )
    try:
        result = orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise UpstreamError("Console TTS voice is invalid JSON", status=502) from exc
    if not isinstance(result, dict):
        raise UpstreamError("Console TTS voice is invalid", status=502)
    return result


async def transcribe_speech(
    token: str,
    *,
    file_data: bytes | None = None,
    file_name: str = "audio.bin",
    file_content_type: str = "application/octet-stream",
    url: str | None = None,
    model: str = "grok-stt",
    fields: dict[str, str] | None = None,
) -> dict[str, Any]:
    has_file = file_data is not None
    has_url = bool(url and url.strip())
    if has_file == has_url:
        raise ValueError("STT requires exactly one of file or url")
    form = {"model": model}
    form.update({key: str(value) for key, value in (fields or {}).items() if str(value).strip()})
    if url:
        form["url"] = url.strip()
    files = None
    if file_data is not None:
        files = {"file": (file_name or "audio.bin", file_data, file_content_type or "application/octet-stream")}
    _, _, body = await request_bytes(
        token,
        "POST",
        _console_url("stt"),
        content_type="",
        accept="application/json",
        files=files,
        form=form,
        timeout_s=get_config().get_float("voice.timeout", 300.0),
    )
    try:
        return orjson.loads(body)
    except orjson.JSONDecodeError as exc:
        raise UpstreamError("Console STT response is invalid", status=502) from exc


__all__ = [
    "edit_image",
    "generate_image",
    "generate_video",
    "localize_video",
    "list_tts_voices",
    "get_tts_voice",
    "synthesize_speech",
    "transcribe_speech",
]
