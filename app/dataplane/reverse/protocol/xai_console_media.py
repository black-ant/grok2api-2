"""Payload and response helpers for the Console media API."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


_IMAGE_RATIOS = {
    "1:1": "1:1",
    "16:9": "16:9",
    "9:16": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
    "2:1": "2:1",
    "1:2": "1:2",
    "1024x1024": "1:1",
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1792x1024": "3:2",
    "1536x1024": "3:2",
    "1024x1792": "2:3",
    "1024x1536": "2:3",
}
_VIDEO_RATIOS = {"1:1", "16:9", "9:16"}
_VIDEO_RESOLUTIONS = {"480p", "720p", "1080p"}
_VOICE_MODELS = {
    "grok-voice-latest",
    "grok-voice-think-fast-1.0",
    "grok-voice-think-fast-2.0",
}
_MEDIA_MODEL_ALIASES = {
    "grok-imagine-image-quality-2.0": "grok-imagine-image-quality",
}


def upstream_media_model(model: str) -> str:
    return _MEDIA_MODEL_ALIASES.get(model.strip(), model.strip())


def normalize_image_format(value: str | None) -> str:
    normalized = (value or "url").strip().lower()
    if normalized not in {"url", "b64_json"}:
        raise ValueError("response_format must be url or b64_json")
    return normalized


def normalize_image_ratio(aspect_ratio: str | None, size: str | None) -> str | None:
    value = (aspect_ratio or size or "").strip().lower()
    if not value or value == "auto":
        return None
    ratio = _IMAGE_RATIOS.get(value)
    if ratio is None:
        raise ValueError("aspect_ratio or size is not supported")
    return ratio


def normalize_image_resolution(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in {"1k", "2k"}:
        raise ValueError("resolution must be 1k or 2k")
    return normalized


def normalize_image_quality(model: str, value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if model not in {"grok-imagine-image-2.0", "grok-imagine-image-quality-2.0"}:
        raise ValueError("quality is only supported by grok-imagine-image-2.0")
    if normalized not in {"low", "medium"}:
        raise ValueError("quality must be low or medium")
    return normalized


def normalize_video_ratio(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in _VIDEO_RATIOS:
        raise ValueError("aspect_ratio must be one of 1:1, 9:16, 16:9")
    return normalized


def normalize_video_resolution(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in _VIDEO_RESOLUTIONS:
        raise ValueError("resolution must be one of 480p, 720p, 1080p")
    return normalized


def validate_https_or_data_url(value: str, media_type: str) -> bool:
    normalized = value.strip()
    lower = normalized.lower()
    if lower.startswith(f"data:{media_type}/"):
        return ";base64," in lower
    parsed = urlparse(normalized)
    return (
        parsed.scheme.lower() == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def build_image_generation_payload(
    *,
    model: str,
    prompt: str,
    count: int,
    response_format: str,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    quality: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": upstream_media_model(model),
        "prompt": prompt,
        "n": count,
        "response_format": normalize_image_format(response_format),
    }
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if resolution:
        payload["resolution"] = resolution
    if quality:
        payload["quality"] = quality
    return payload


def build_image_edit_payload(
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
    if not 1 <= len(image_urls) <= 3:
        raise ValueError("Console image editing accepts 1 to 3 images")
    images = [{"type": "image_url", "url": item.strip()} for item in image_urls]
    payload: dict[str, Any] = {
        "model": upstream_media_model(model),
        "prompt": prompt,
        "n": count,
        "response_format": normalize_image_format(response_format),
    }
    payload["image" if len(images) == 1 else "images"] = images[0] if len(images) == 1 else images
    if aspect_ratio:
        payload["aspect_ratio"] = aspect_ratio
    if resolution:
        payload["resolution"] = resolution
    if quality:
        payload["quality"] = quality
    return payload


def build_video_generation_payload(
    *,
    model: str,
    prompt: str,
    duration: int,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    image_url: str | None = None,
    reference_urls: list[str] | None = None,
    reference_audios: list[str] | None = None,
) -> dict[str, Any]:
    if not 1 <= duration <= 15:
        raise ValueError("duration must be between 1 and 15 seconds")
    if image_url and (reference_urls or reference_audios):
        raise ValueError("image cannot be combined with reference_images or reference_audios")
    if len(reference_urls or ()) > 7:
        raise ValueError("at most 7 reference_images are supported")
    if len(reference_audios or ()) > 3:
        raise ValueError("at most 3 reference_audios are supported")
    if reference_urls and model == "grok-imagine-video" and duration > 10:
        raise ValueError("reference-to-video is limited to 10 seconds on grok-imagine-video")
    normalized_resolution = normalize_video_resolution(resolution)
    if normalized_resolution == "1080p":
        if model != "grok-imagine-video-1.5":
            raise ValueError("1080p is only supported by grok-imagine-video-1.5")
        if reference_urls:
            raise ValueError("reference_images mode is limited to 720p; 1080p is not supported")
    payload: dict[str, Any] = {"model": upstream_media_model(model), "duration": duration}
    if prompt.strip():
        payload["prompt"] = prompt.strip()
    if aspect_ratio:
        payload["aspect_ratio"] = normalize_video_ratio(aspect_ratio)
    if normalized_resolution:
        payload["resolution"] = normalized_resolution
    if image_url:
        payload["image"] = {"url": image_url.strip()}
    if reference_urls:
        payload["reference_images"] = [{"url": item.strip()} for item in reference_urls]
    if reference_audios:
        payload["reference_audios"] = [{"voice_id": item.strip()} for item in reference_audios]
    if "prompt" not in payload and "image" not in payload:
        raise ValueError("prompt is required unless an image is supplied")
    return payload


def build_video_edit_payload(*, model: str, prompt: str, video_url: str) -> dict[str, Any]:
    if not prompt.strip():
        raise ValueError("prompt is required")
    return {"model": model, "prompt": prompt.strip(), "video": {"url": video_url.strip()}}


def parse_video_create(body: bytes) -> str:
    payload = json.loads(body or b"{}")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise ValueError("Console video response has no request_id")
    return request_id


def parse_video_status(body: bytes) -> tuple[str, int, str | None, str | None]:
    payload = json.loads(body or b"{}")
    status = str(payload.get("status") or "").strip().lower()
    try:
        progress = max(0, min(100, int(payload.get("progress") or 0)))
    except (TypeError, ValueError):
        progress = 0
    video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    url = str(video.get("url") or "").strip() or None
    error = payload.get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error.get("code")
    error_text = str(error).strip() if error is not None else None
    return status, progress, url, error_text


def safe_error_text(value: object, limit: int = 160) -> str:
    if isinstance(value, dict):
        for key in ("message", "msg", "code", "type", "detail", "error_description"):
            if key in value:
                text = safe_error_text(value[key], limit)
                if text:
                    return text
    if isinstance(value, list):
        for item in value:
            text = safe_error_text(item, limit)
            if text:
                return text
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    if any(secret in lowered for secret in ("authorization", "cookie", "bearer ", "access_token", "sso-rw", "cf_clearance")):
        return "upstream rejected the request"
    return text[:limit]


def is_voice_model(model: str) -> bool:
    return model in _VOICE_MODELS


__all__ = [
    "build_image_edit_payload",
    "build_image_generation_payload",
    "build_video_edit_payload",
    "build_video_generation_payload",
    "is_voice_model",
    "normalize_image_format",
    "normalize_image_quality",
    "normalize_image_ratio",
    "normalize_image_resolution",
    "normalize_video_ratio",
    "normalize_video_resolution",
    "parse_video_create",
    "parse_video_status",
    "safe_error_text",
    "upstream_media_model",
    "validate_https_or_data_url",
]
