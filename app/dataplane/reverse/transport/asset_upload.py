"""Asset upload transport — multipart upload to Grok.

Calls POST ``/http/upload-file-v2/direct`` with a multipart body and returns
the file metadata ID used as a file attachment reference in chat and
image-edit. The legacy base64 ``/rest/app-chat/upload-file`` endpoint is gone:
grok.com rejects it before the application layer answers, which surfaces as a
bare 403 with an empty body.
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
from curl_cffi import CurlMime

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

_UPLOAD_URL = "https://grok.com/http/upload-file-v2/direct"
# Imagine image-edit uploads must declare where the browser started them; the
# chat flow sends no file_source at all, matching the web client.
FILE_SOURCE_IMAGINE_SELF = "IMAGINE_SELF_UPLOAD_FILE_SOURCE"
IMAGINE_REFERER = "https://grok.com/imagine"
_X_USER_ID_RE = re.compile(r"(?:^|;\s*)x-userid=([^;]+)")
_FILENAME_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
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

def _is_challenge_403(exc: UpstreamError) -> bool:
    """Tell a browser-session challenge apart from a policy rejection.

    grok.com answers an unusable browser session with 403 before the
    application layer is built: the body is empty or a Cloudflare HTML page.
    Structured JSON 403s are policy decisions and must not be replayed.
    """
    if exc.status != 403:
        return False
    body = str(exc.details.get("body") or "").strip()
    if not body:
        return True
    head = body[:400].lower()
    return body.startswith("<") or "just a moment" in head or "<html" in head


def _safe_filename(filename: str) -> str:
    cleaned = _FILENAME_CONTROL_RE.sub("_", filename or "").strip()
    return cleaned or "upload.bin"


def _build_upload_form(
    filename:    str,
    mime:        str,
    raw:         bytes,
    file_source: str | None,
) -> CurlMime:
    form = CurlMime()
    form.addpart(
        name         = "file",
        content_type = mime,
        filename     = _safe_filename(filename),
        data         = raw,
    )
    if file_source:
        form.addpart(name="file_source", data=file_source.encode("utf-8"))
    return form


def _terminal_error_text(terminal: object) -> str:
    """Flatten a v2 terminal error into one readable line."""
    if isinstance(terminal, dict):
        for key in ("message", "error", "code", "reason"):
            value = str(terminal.get(key) or "").strip()
            if value:
                return value[:160]
        return ""
    if isinstance(terminal, str):
        return terminal.strip()[:160]
    return ""


def _parse_upload_response(body_bytes: bytes) -> tuple[str, str]:
    """Return ``(file_id, file_uri)`` from an upload-file-v2 response.

    A 2xx status only means the gateway accepted the request; the body still has
    to carry a file reference. Every other shape is reported as a 502 so the
    client sees a failed upload instead of a successful response with an error
    object inside.
    """
    try:
        result = orjson.loads(body_bytes)
    except orjson.JSONDecodeError as exc:
        raise UpstreamError(
            "Asset upload response was not valid JSON",
            status = 502,
            body   = body_bytes.decode("utf-8", "replace")[:300],
        ) from exc
    if not isinstance(result, dict):
        raise UpstreamError("Asset upload response was not an object", status=502)

    terminal = result.get("terminalError")
    if terminal not in (None, "", False, 0, {}, []):
        reason = _terminal_error_text(terminal)
        raise UpstreamError(
            f"Asset upload was rejected upstream: {reason}" if reason
            else "Asset upload was rejected upstream",
            status = 502,
            body   = orjson.dumps(terminal).decode("utf-8", "replace")[:300],
        )

    metadata = result.get("fileMetadata")
    if not isinstance(metadata, dict):
        metadata = {}
    # fileMetadataId is what image-edit needs; chat also accepts the upload task
    # id when the metadata record is still being built asynchronously.
    file_id = (
        str(metadata.get("fileMetadataId") or "")
        or str(metadata.get("fileId") or "")
        or str(result.get("uploadId") or "")
        or str(result.get("fileMetadataId") or "")
    )
    file_uri = str(metadata.get("fileUri") or result.get("fileUri") or "")
    if not file_id and not file_uri:
        raise UpstreamError("Asset upload returned no file reference", status=502)
    return file_id, file_uri


async def upload_file(
    token:       str,
    filename:    str,
    mime:        str,
    b64:         str,
    *,
    file_source: str | None = None,
    referer:     str | None = None,
) -> tuple[str, str]:
    """Upload base64-encoded file content to Grok.

    Args:
        token:       SSO session token.
        filename:    Original file name (sent to upstream as the part name).
        mime:        MIME type string (e.g. ``"image/png"``).
        b64:         Base64-encoded file content (no data-URI prefix).
        file_source: Upload flow tag; chat uploads leave it empty.
        referer:     Page the upload was started from (image-edit uses ``/imagine``).

    Returns:
        ``(file_id, file_uri)`` — file_id is used as a file attachment ref.

    Raises:
        ``UpstreamError`` on HTTP failure.
    """
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationError("File input payload is not valid base64", param="content") from exc
    _validate_decoded_size(len(raw))

    async with _get_upload_sem():
        try:
            return await _upload_file_inner(
                token, filename, mime, raw, file_source, referer
            )
        except UpstreamError as exc:
            if not _is_challenge_403(exc):
                raise
            # The failed attempt already invalidated the clearance bundle and
            # rotated the egress node through proxy feedback, so one retry runs
            # on a fresh browser session instead of the rejected one.
            logger.warning(
                "asset upload hit a challenge 403; retrying with a fresh lease: filename={!r}",
                filename,
            )
            return await _upload_file_inner(
                token, filename, mime, raw, file_source, referer
            )


async def _upload_file_inner(
    token:       str,
    filename:    str,
    mime:        str,
    raw:         bytes,
    file_source: str | None,
    referer:     str | None,
) -> tuple[str, str]:
    cfg       = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    headers = build_http_headers(token, referer=referer, lease=lease)
    # The multipart body supplies its own Content-Type with the boundary, and
    # grok.com rejects the upload when the legacy request id header is present.
    headers.pop("Content-Type", None)
    headers.pop("x-xai-request-id", None)
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"]        = "no-cache"
    kwargs = build_session_kwargs(lease=lease)

    form = _build_upload_form(filename, mime, raw, file_source)
    try:
        try:
            async with ResettableSession(**kwargs) as session:
                response = await session.post(
                    _UPLOAD_URL,
                    headers   = headers,
                    multipart = form,
                    timeout   = timeout_s,
                )
        finally:
            form.close()
    except UpstreamError:
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        raise UpstreamError(f"Asset upload transport error: {exc}") from exc

    body_bytes = response.content
    if not 200 <= response.status_code < 300:
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
        ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=response.status_code),
    )

    file_id, file_uri = _parse_upload_response(body_bytes)
    logger.info("asset upload completed: filename={!r} file_id={}", filename, file_id)
    return file_id, file_uri


async def upload_from_input(
    token: str,
    file_input: str,
    *,
    allowed_mime_prefixes: tuple[str, ...] | None = None,
    file_source: str | None = None,
    referer: str | None = None,
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
                return await upload_file(
                    token,
                    filename,
                    mime,
                    b64,
                    file_source=file_source,
                    referer=referer,
                )

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
    return await upload_file(
        token, filename, mime, b64, file_source=file_source, referer=referer
    )


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
    "FILE_SOURCE_IMAGINE_SELF",
    "IMAGINE_REFERER",
    "upload_file",
    "upload_from_input",
    "parse_data_uri",
    "resolve_uploaded_asset_reference",
]
