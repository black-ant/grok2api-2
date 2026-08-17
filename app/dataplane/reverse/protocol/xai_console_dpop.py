"""DPoP session and request helpers for the Console API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind, ProxyLease
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_console_headers
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError, parse_retry_after
from app.platform.logging.logger import logger


_REFRESH_SKEW_S = 20
_MAX_TOKEN_LIFETIME_S = 3600
_CACHE: dict[str, "DpopSession"] = {}
_CACHE_LOCK = asyncio.Lock()
_LOAD_LOCKS: dict[str, asyncio.Lock] = {}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_jwk(private_key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    public = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(public.x.to_bytes(32, "big")),
        "y": _b64url(public.y.to_bytes(32, "big")),
    }


def _thumbprint(jwk: dict[str, str]) -> str:
    canonical = json.dumps(
        {key: jwk[key] for key in ("crv", "kty", "x", "y")},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _b64url(hashlib.sha256(canonical).digest())


def _cache_key(token: str, lease: ProxyLease | None) -> str:
    affinity = (lease.proxy_url if lease is not None else "") or "direct"
    user_agent = lease.user_agent if lease is not None else ""
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"{affinity}|{user_agent}|{digest}"


@dataclass(slots=True, frozen=True)
class DpopSession:
    access_token: str
    private_key: ec.EllipticCurvePrivateKey
    public_jwk: dict[str, str]
    expires_at: float
    clock_skew_s: float = 0.0


def _session_valid(session: DpopSession) -> bool:
    return session.expires_at > time.time() + _REFRESH_SKEW_S


async def _feedback(proxy, lease: ProxyLease, status: int | None, *, transport: bool = False) -> None:
    if transport:
        kind = ProxyFeedbackKind.TRANSPORT_ERROR
    elif status == 403:
        kind = ProxyFeedbackKind.CHALLENGE
    elif status == 401:
        kind = ProxyFeedbackKind.UNAUTHORIZED
    elif status == 429:
        kind = ProxyFeedbackKind.RATE_LIMITED
    elif status is not None and status >= 500:
        kind = ProxyFeedbackKind.UPSTREAM_5XX
    else:
        kind = ProxyFeedbackKind.FORBIDDEN
    try:
        await proxy.feedback(lease, ProxyFeedback(kind=kind, status_code=status))
    except Exception as exc:
        logger.debug("console dpop proxy feedback failed: error={}", exc)


async def _mint_session(token: str, lease: ProxyLease, base_url: str) -> DpopSession:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = _public_jwk(private_key)
    endpoint = f"{base_url.rstrip('/')}/v1/dpop/token"
    headers = build_console_headers(token, lease=lease)
    headers.pop("Authorization", None)
    headers["Content-Type"] = "application/json"
    payload = json.dumps({"jwk": public_jwk}, separators=(",", ":")).encode("utf-8")
    local_before = datetime.now(UTC)

    async with ResettableSession(**build_session_kwargs(lease=lease)) as session:
        try:
            response = await session.post(
                endpoint,
                headers=headers,
                data=payload,
                timeout=get_config().get_float("console.timeout", 120.0),
            )
            body = bytes(response.content or b"")
        except Exception:
            raise
    local_after = datetime.now(UTC)
    if response.status_code < 200 or response.status_code >= 300:
        body_text = body.decode("utf-8", "replace")[:400]
        raise UpstreamError(
            f"Console DPoP token endpoint returned {response.status_code}",
            status=response.status_code,
            body=body_text,
            retry_after_s=parse_retry_after(response.headers.get("Retry-After")),
        )

    try:
        payload_obj = json.loads(body or b"{}")
        access_token = str(payload_obj.get("access_token") or "").strip()
        token_type = str(payload_obj.get("token_type") or "").strip().lower()
        expires_in = int(payload_obj.get("expires_in") or 0)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UpstreamError("Invalid Console DPoP token response", status=502) from exc
    if not access_token or token_type != "dpop":
        raise UpstreamError("Invalid Console DPoP token response", status=502)
    if not 0 < expires_in <= _MAX_TOKEN_LIFETIME_S:
        raise UpstreamError("Invalid Console DPoP token lifetime", status=502)

    parts = access_token.split(".")
    if len(parts) != 3:
        raise UpstreamError("Invalid Console DPoP access token", status=502)
    try:
        claims = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise UpstreamError("Invalid Console DPoP access token claims", status=502) from exc
    token_exp = int(claims.get("exp") or 0)
    token_thumbprint = str((claims.get("cnf") or {}).get("jkt") or "")
    if token_exp <= 0 or token_thumbprint != _thumbprint(public_jwk):
        raise UpstreamError("Console DPoP token key binding mismatch", status=502)

    server_date = response.headers.get("Date", "")
    clock_skew_s = 0.0
    if server_date:
        try:
            server_dt = datetime.strptime(server_date, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)
            midpoint = local_before + (local_after - local_before) / 2
            clock_skew_s = round((server_dt - midpoint).total_seconds())
        except ValueError:
            clock_skew_s = 0.0
    expires_at = min(time.time() + expires_in, float(token_exp))
    if expires_at <= time.time() + _REFRESH_SKEW_S:
        raise UpstreamError("Console DPoP token is expired", status=502)
    return DpopSession(
        access_token=access_token,
        private_key=private_key,
        public_jwk=public_jwk,
        expires_at=expires_at,
        clock_skew_s=clock_skew_s,
    )


async def get_session(token: str, lease: ProxyLease, *, base_url: str) -> DpopSession:
    key = _cache_key(token, lease)
    async with _CACHE_LOCK:
        current = _CACHE.get(key)
        if current is not None and _session_valid(current):
            return current
        lock = _LOAD_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        async with _CACHE_LOCK:
            current = _CACHE.get(key)
            if current is not None and _session_valid(current):
                return current
        session = await _mint_session(token, lease, base_url)
        async with _CACHE_LOCK:
            _CACHE[key] = session
        return session


async def invalidate(token: str, lease: ProxyLease, session: DpopSession) -> None:
    key = _cache_key(token, lease)
    async with _CACHE_LOCK:
        current = _CACHE.get(key)
        if current is session or (current is not None and current.access_token == session.access_token):
            _CACHE.pop(key, None)


def build_proof(session: DpopSession, *, method: str, url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or "/"
    scheme = parsed.scheme.lower()
    if scheme == "wss":
        scheme = "https"
    elif scheme == "ws":
        scheme = "http"
    htu = f"{scheme}://{parsed.netloc}{path}"
    claims = {
        "jti": str(uuid.uuid4()),
        "htm": method.upper(),
        "htu": htu,
        "iat": int(time.time() + session.clock_skew_s),
        "ath": _b64url(hashlib.sha256(session.access_token.encode("utf-8")).digest()),
    }
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": session.public_jwk}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    der_signature = session.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(der_signature)
    signature = _b64url(r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big"))
    return f"{encoded_header}.{encoded_claims}.{signature}"


async def request_bytes(
    token: str,
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    content_type: str = "application/json",
    accept: str = "*/*",
    files: dict[str, tuple[str, bytes, str]] | None = None,
    form: dict[str, str] | None = None,
    timeout_s: float | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Execute one Console request and materialize its response body."""
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    base_url = get_config().get_str("console.base_url", "https://console.x.ai").rstrip("/")
    timeout = timeout_s or get_config().get_float("console.timeout", 120.0)
    method_upper = method.upper()
    last_error: UpstreamError | None = None
    for attempt in range(2):
            session = await get_session(token, lease, base_url=base_url)
            headers = build_console_headers(token, lease=lease, content_type=content_type)
            headers["Authorization"] = f"DPoP {session.access_token}"
            headers["DPoP"] = build_proof(session, method=method_upper, url=url)
            headers["Accept"] = accept
            if files is not None:
                headers.pop("Content-Type", None)
            if urlsplit(url).path.endswith("/responses"):
                headers["x-cluster"] = "https://us-east-1.api.x.ai"

            kwargs: dict[str, Any] = {"headers": headers, "timeout": timeout}
            if files is not None:
                kwargs["files"] = files
                kwargs["data"] = form or {}
            elif body is not None:
                kwargs["data"] = body
            elif form is not None:
                kwargs["data"] = form

            async with ResettableSession(**build_session_kwargs(lease=lease)) as client:
                try:
                    if method_upper == "GET":
                        response = await client.get(url, **kwargs)
                    elif method_upper == "POST":
                        response = await client.post(url, **kwargs)
                    elif method_upper == "DELETE":
                        response = await client.delete(url, **kwargs)
                    else:
                        raise ValueError(f"Unsupported Console method: {method_upper}")
                    status = int(response.status_code)
                    response_headers = {str(k): str(v) for k, v in response.headers.items()}
                    response_body = bytes(response.content or b"")
                except UpstreamError:
                    raise
                except Exception as exc:
                    await _feedback(proxy, lease, None, transport=True)
                    raise UpstreamError(f"Console transport failed: {exc}", status=502) from exc

            if status == 401 and attempt == 0:
                await invalidate(token, lease, session)
                continue
            if 200 <= status < 300:
                await _feedback(proxy, lease, status)
                return status, response_headers, response_body
            body_text = response_body.decode("utf-8", "replace")[:400]
            last_error = UpstreamError(
                f"Console API returned {status}",
                status=status,
                body=body_text,
                retry_after_s=parse_retry_after(response_headers.get("Retry-After")),
            )
            await _feedback(proxy, lease, status)
            if status == 401 and attempt == 0:
                await invalidate(token, lease, session)
                continue
            raise last_error

    raise last_error or UpstreamError("Console request failed", status=502)


__all__ = [
    "DpopSession",
    "build_proof",
    "get_session",
    "invalidate",
    "request_bytes",
]
