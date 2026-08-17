"""Asset upload transport — direct base64 upload to Grok.

Calls POST /rest/app-chat/upload-file with base64-encoded content and
returns the file metadata ID used as a file attachment reference in chat.
"""

import asyncio
import base64
import binascii
import ipaddress
import mimetypes
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError, ValidationError
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_sso_cookie
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.dataplane.reverse.protocol.xai_assets import resolve_asset_reference
from app.control.proxy.feedback import build_feedback
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

_UPLOAD_URL = "https://grok.com/rest/app-chat/upload-file"
_X_USER_ID_RE = re.compile(r"(?:^|;\s*)x-userid=([^;]+)")
_DEFAULT_INPUT_MAX_BYTES = 20 * 1024 * 1024
_DEFAULT_INPUT_TIMEOUT_S = 30.0
_DEFAULT_MAX_REDIRECTS = 5

# Global semaphore — limits concurrent upload_file() calls across all requests.
# Initialised lazily on first call so the event loop is guaranteed to be running.
_upload_sem: asyncio.Semaphore | None = None

def _get_upload_sem() -> asyncio.Semaphore:
    global _upload_sem
    if _upload_sem is None:
        n = max(1, int(get_config("batch.asset_upload_concurrency", 10)))
        _upload_sem = asyncio.Semaphore(n)
    return _upload_sem


# ---------------------------------------------------------------------------
# File-input parsing
# ---------------------------------------------------------------------------

def _is_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return bool(p.scheme in {"http", "https"} and p.netloc)
    except Exception:
        return False


def _input_max_bytes() -> int:
    configured_mb = get_config("asset.input_max_mb", 20)
    try:
        megabytes = max(1, int(configured_mb))
    except (TypeError, ValueError):
        megabytes = _DEFAULT_INPUT_MAX_BYTES // (1024 * 1024)
    return megabytes * 1024 * 1024


def _validate_mime(
    mime: str,
    filename: str,
    allowed_mime_prefixes: tuple[str, ...] | None,
) -> str:
    normalized = (mime or "application/octet-stream").split(";", 1)[0].strip().lower()
    if normalized in {"", "application/octet-stream"}:
        guessed = _mime_from_name(filename, "")
        if guessed:
            normalized = guessed
    if not allowed_mime_prefixes:
        return normalized

    if any(normalized.startswith(prefix.lower()) for prefix in allowed_mime_prefixes):
        return normalized

    allowed = ", ".join(allowed_mime_prefixes)
    raise ValidationError(
        f"Input file must use one of these MIME families: {allowed}",
        param="content",
    )


def _validate_decoded_size(size: int) -> None:
    max_bytes = _input_max_bytes()
    if size > max_bytes:
        limit_mb = max_bytes // (1024 * 1024)
        raise ValidationError(
            f"Input file exceeds the {limit_mb} MB limit",
            param="content",
        )


def _validate_public_host(host: str) -> None:
    normalized = host.rstrip(".").lower()
    if (
        normalized in {"localhost", "localhost.localdomain"}
        or normalized.endswith((".localhost", ".local", ".internal"))
    ):
        raise ValidationError("Input URL must resolve to a public host", param="content")

    try:
        addresses = [ipaddress.ip_address(normalized)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValidationError(
                "Input URL host could not be resolved",
                param="content",
            ) from exc
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except (IndexError, ValueError):
                continue

    if not addresses or any(not address.is_global for address in addresses):
        raise ValidationError("Input URL must resolve to a public host", param="content")


async def _validate_remote_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValidationError("File input must be an HTTP(S) URL or data URI", param="content")
    if parsed.username or parsed.password:
        raise ValidationError("Input URL credentials are not supported", param="content")
    await asyncio.to_thread(_validate_public_host, parsed.hostname)


async def _read_limited_response(response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_content():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            limit_mb = max_bytes // (1024 * 1024)
            raise ValidationError(
                f"Input file exceeds the {limit_mb} MB limit",
                param="content",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _mime_from_name(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def parse_data_uri(data_uri: str) -> tuple[str, str, str]:
    """Split a data URI into (filename, base64_content, mime_type).

    Raises ``ValidationError`` on invalid input.
    """
    if not data_uri.startswith("data:"):
        raise ValidationError("File input must be a URL or data URI", param="content")

    try:
        header, b64 = data_uri.split(",", 1)
    except ValueError:
        raise ValidationError("Malformed data URI: missing comma separator", param="content")

    if ";base64" not in header:
        raise ValidationError("Data URI must be base64-encoded", param="content")

    mime = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
    b64  = re.sub(r"\s+", "", b64)
    if not b64:
        raise ValidationError("Data URI has empty payload", param="content")

    try:
        decoded_size = len(base64.b64decode(b64, validate=True))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationError("Data URI payload is not valid base64", param="content") from exc
    _validate_decoded_size(decoded_size)

    ext  = mime.split("/")[-1] if "/" in mime else "bin"
    return f"file.{ext}", b64, mime


# ---------------------------------------------------------------------------
# Core upload function
# ---------------------------------------------------------------------------

async def upload_file(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    """Upload base64-encoded file content to Grok.

    Args:
        token:    SSO session token.
        filename: Original file name (used for content-type inference).
        mime:     MIME type string (e.g. ``"image/png"``).
        b64:      Base64-encoded file content (no data-URI prefix).

    Returns:
        ``(file_id, file_uri)`` — file_id is used as a file attachment ref.

    Raises:
        ``UpstreamError`` on HTTP failure.
    """
    try:
        _validate_decoded_size(len(base64.b64decode(b64, validate=True)))
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationError("File input payload is not valid base64", param="content") from exc
    async with _get_upload_sem():
        return await _upload_file_inner(token, filename, mime, b64)


async def _upload_file_inner(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    cfg       = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    payload = orjson.dumps({
        "fileName":     filename,
        "fileMimeType": mime,
        "content":      b64,
    })
    headers = build_http_headers(token, lease=lease)
    kwargs  = build_session_kwargs(lease=lease)

    try:
        async with ResettableSession(**kwargs) as session:
            response = await session.post(
                _UPLOAD_URL,
                headers = headers,
                data    = payload,
                timeout = timeout_s,
            )

        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:300]
            logger.error(
                "asset upload request failed: status={} body={}",
                response.status_code, body_text,
            )
            is_cloudflare = "just a moment" in body_text.lower()
            await proxy.feedback(
                lease,
                build_feedback(response.status_code, is_cloudflare=is_cloudflare),
            )
            raise UpstreamError(
                f"Asset upload returned {response.status_code}",
                status = response.status_code,
                body   = body_text,
            )

        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )

        result   = orjson.loads(body_bytes)
        file_id  = result.get("fileMetadataId") or result.get("fileId", "")
        file_uri = result.get("fileUri", "")
        logger.info("asset upload completed: filename={!r} file_id={}", filename, file_id)
        return file_id, file_uri

    except UpstreamError:
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        raise UpstreamError(f"Asset upload transport error: {exc}") from exc


async def upload_from_input(
    token: str,
    file_input: str,
    *,
    allowed_mime_prefixes: tuple[str, ...] | None = None,
) -> tuple[str, str]:
    """High-level helper: parse *file_input* (URL or data URI) and upload.

    Returns ``(file_id, file_uri)``.
    """
    if _is_url(file_input):
        max_bytes = _input_max_bytes()
        cfg = get_config()
        timeout_s = cfg.get_float("asset.input_timeout", _DEFAULT_INPUT_TIMEOUT_S)
        max_redirects = max(
            0,
            min(10, cfg.get_int("asset.max_redirects", _DEFAULT_MAX_REDIRECTS)),
        )
        current_url = file_input
        proxy = await get_proxy_runtime()
        lease = await proxy.acquire()
        lease_reported = False
        try:
            for hop in range(max_redirects + 1):
                await _validate_remote_url(current_url)
                headers = build_http_headers(token, lease=lease)
                kwargs = build_session_kwargs(lease=lease)
                async with ResettableSession(**kwargs) as session:
                    resp = await session.get(
                        current_url,
                        headers=headers,
                        timeout=timeout_s,
                        stream=True,
                        allow_redirects=False,
                    )
                    if 300 <= resp.status_code < 400:
                        location = str(resp.headers.get("location") or "").strip()
                        if not location:
                            raise UpstreamError(
                                "Input URL redirect has no location",
                                status=resp.status_code,
                            )
                        if hop >= max_redirects:
                            raise ValidationError(
                                "Input URL has too many redirects",
                                param="content",
                            )
                        current_url = urljoin(current_url, location)
                        continue

                    if resp.status_code != 200:
                        raise UpstreamError(
                            f"Failed to fetch input URL: {resp.status_code}",
                            status=resp.status_code,
                        )

                    content_length = str(resp.headers.get("content-length") or "").strip()
                    if content_length.isdigit() and int(content_length) > max_bytes:
                        raise ValidationError(
                            f"Input file exceeds the {max_bytes // (1024 * 1024)} MB limit",
                            param="content",
                        )
                    raw = await _read_limited_response(resp, max_bytes)
                    mime = str(resp.headers.get("content-type") or "").split(";", 1)[0].strip()
                    filename = Path(unquote(urlparse(current_url).path)).name or "download"
                    mime = _validate_mime(mime or _mime_from_name(filename), filename, allowed_mime_prefixes)
                    if not raw:
                        raise ValidationError("Input file cannot be empty", param="content")
                    b64 = base64.b64encode(raw).decode("ascii")

                await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
                lease_reported = True
                return await upload_file(token, filename, mime, b64)

            raise ValidationError("Input URL has too many redirects", param="content")
        except ValidationError:
            if not lease_reported:
                await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
            raise
        except UpstreamError as exc:
            if not lease_reported:
                await proxy.feedback(
                    lease,
                    ProxyFeedback(
                        kind=ProxyFeedbackKind.UPSTREAM_5XX
                        if exc.status >= 500
                        else ProxyFeedbackKind.FORBIDDEN,
                        status_code=exc.status,
                    ),
                )
            raise
        except Exception as exc:
            if not lease_reported:
                await proxy.feedback(
                    lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR)
                )
            raise UpstreamError(f"Asset fetch transport error: {exc}") from exc

    # Data URI
    filename, b64, mime = parse_data_uri(file_input)
    mime = _validate_mime(mime, filename, allowed_mime_prefixes)
    return await upload_file(token, filename, mime, b64)


def resolve_uploaded_asset_reference(token: str, file_id: str, file_uri: str) -> str:
    """Resolve an uploaded asset to the content URL required by image-edit."""
    user_id = _extract_user_id(token)
    url = resolve_asset_reference(file_id, file_uri, user_id=user_id)
    if url:
        return url
    raise UpstreamError("Could not resolve uploaded asset reference URL")


def _extract_user_id(token: str) -> str | None:
    cookie = build_sso_cookie(token)
    match = _X_USER_ID_RE.search(cookie)
    if match:
        return match.group(1)
    return None


__all__ = [
    "upload_file",
    "upload_from_input",
    "parse_data_uri",
    "resolve_uploaded_asset_reference",
]
