"""File-backed HTTP request log with daily retention."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import orjson
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.platform.logging.logger import logger
from app.platform.paths import log_path


_DEFAULT_RETENTION_DAYS = 2
_DEFAULT_BODY_LIMIT = 64 * 1024
_DEFAULT_PATH_PREFIXES = ("/v1", "/webui/api")
_LOG_FILE_PREFIX = "request_"
_LOG_FILE_SUFFIX = ".jsonl"

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "app_key",
    "cookie",
    "password",
    "secret",
    "token",
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_prefixes() -> tuple[str, ...]:
    raw = os.getenv("REQUEST_LOG_PATH_PREFIXES")
    if not raw:
        return _DEFAULT_PATH_PREFIXES
    prefixes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return prefixes or _DEFAULT_PATH_PREFIXES


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***REDACTED***" if _is_sensitive_key(str(key)) else _redact_json(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def _decode_header(value: bytes) -> str:
    return value.decode("latin-1", "replace")


def _headers_dict(raw_headers: list[tuple[bytes, bytes]]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw_key, raw_value in raw_headers:
        key = _decode_header(raw_key).lower()
        value = _decode_header(raw_value)
        headers[key] = "***REDACTED***" if _is_sensitive_key(key) else value
    return headers


def _query_params(query_string: bytes) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in parse_qsl(query_string.decode("utf-8", "replace"), keep_blank_values=True):
        safe_value = "***REDACTED***" if _is_sensitive_key(key) else value
        if key in params:
            current = params[key]
            if isinstance(current, list):
                current.append(safe_value)
            else:
                params[key] = [current, safe_value]
        else:
            params[key] = safe_value
    return params


def _is_json_content_type(content_type: str) -> bool:
    lowered = content_type.lower()
    return "application/json" in lowered or "+json" in lowered


def _is_text_content_type(content_type: str) -> bool:
    lowered = content_type.lower()
    return (
        lowered.startswith("text/")
        or "application/x-www-form-urlencoded" in lowered
        or "application/xml" in lowered
        or "application/javascript" in lowered
        or "application/problem+json" in lowered
    )


def _body_payload(
    body: bytes,
    *,
    content_type: str,
    total_bytes: int,
    truncated: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content_type": content_type or "",
        "bytes": total_bytes,
        "truncated": truncated,
        "body": None,
    }
    if not body:
        return payload

    if _is_json_content_type(content_type):
        try:
            payload["body"] = _redact_json(orjson.loads(body))
            return payload
        except orjson.JSONDecodeError:
            pass

    text = body.decode("utf-8", "replace")
    if "application/x-www-form-urlencoded" in content_type.lower():
        form: dict[str, Any] = {}
        for key, value in parse_qsl(text, keep_blank_values=True):
            form[key] = "***REDACTED***" if _is_sensitive_key(key) else value
        payload["body"] = form
        return payload

    if _is_text_content_type(content_type) or "multipart/form-data" in content_type.lower():
        payload["body"] = text
        return payload

    payload["body"] = {
        "preview": text,
        "note": "binary or unknown content type; preview is truncated and lossy",
    }
    return payload


def _path_matches(path: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        normalized = prefix.rstrip("/") or "/"
        if path == normalized or path.startswith(normalized + "/"):
            return True
    return False


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _local_log_date() -> str:
    return date.today().isoformat()


class RequestLogStore:
    def __init__(
        self,
        *,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        directory: Path | None = None,
    ) -> None:
        self.retention_days = max(1, retention_days)
        self._directory = directory
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> Path:
        return self._directory or log_path("requests")

    def configure(self, *, retention_days: int = _DEFAULT_RETENTION_DAYS) -> None:
        self.retention_days = max(1, retention_days)

    def retained_dates(self) -> list[str]:
        today = date.today()
        return [
            (today - timedelta(days=offset)).isoformat()
            for offset in range(self.retention_days)
        ]

    def _path_for_date(self, log_date: str | date) -> Path:
        value = log_date.isoformat() if isinstance(log_date, date) else log_date
        return self.directory / f"{_LOG_FILE_PREFIX}{value}{_LOG_FILE_SUFFIX}"

    def _cleanup_locked(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        retained = set(self.retained_dates())
        for path in self.directory.glob(f"{_LOG_FILE_PREFIX}*{_LOG_FILE_SUFFIX}"):
            log_date = path.stem.removeprefix(_LOG_FILE_PREFIX)
            if log_date not in retained:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _write_entry_locked(self, entry: dict[str, Any]) -> None:
        self._cleanup_locked()
        log_date = str(entry.get("log_date") or date.today().isoformat())
        target = self._path_for_date(log_date)
        payload = orjson.dumps(entry) + b"\n"
        fd = os.open(str(target), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)

    def _read_items_locked(self, *, limit: int) -> list[dict[str, Any]]:
        self._cleanup_locked()
        items: list[dict[str, Any]] = []
        for log_date in self.retained_dates():
            path = self._path_for_date(log_date)
            if not path.exists():
                continue
            try:
                with path.open("rb") as file:
                    lines = deque(file, maxlen=limit)
            except FileNotFoundError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    item = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        items.sort(key=lambda item: float(item.get("created_ts") or 0), reverse=True)
        return items[:limit]

    def _count_locked(self) -> int:
        self._cleanup_locked()
        count = 0
        for log_date in self.retained_dates():
            path = self._path_for_date(log_date)
            if not path.exists():
                continue
            try:
                with path.open("rb") as file:
                    count += sum(1 for line in file if line.strip())
            except FileNotFoundError:
                continue
        return count

    async def add(self, entry: dict[str, Any]) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_entry_locked, entry)

    async def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._read_items_locked, limit=limit)

    async def count(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._count_locked)


request_log_store = RequestLogStore(retention_days=_DEFAULT_RETENTION_DAYS)


class RequestLogMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        body_limit: int | None = None,
        retention_days: int | None = None,
        path_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self.app = app
        self.body_limit = body_limit or _env_int("REQUEST_LOG_BODY_LIMIT", _DEFAULT_BODY_LIMIT)
        self.path_prefixes = path_prefixes or _env_prefixes()
        request_log_store.configure(
            retention_days=retention_days or _DEFAULT_RETENTION_DAYS,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not _path_matches(str(scope.get("path") or ""), self.path_prefixes):
            await self.app(scope, receive, send)
            return

        request_body = bytearray()
        request_bytes = 0
        request_truncated = False
        response_body = bytearray()
        response_bytes = 0
        response_truncated = False
        response_status = 500
        response_headers: dict[str, str] = {}
        error: str | None = None
        started_ts = time.time()
        started_at = _utc_now_iso()
        log_date = _local_log_date()
        started_monotonic = time.perf_counter()

        async def logging_receive() -> Message:
            nonlocal request_bytes, request_truncated
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                request_bytes += len(chunk)
                if chunk and len(request_body) < self.body_limit:
                    remaining = self.body_limit - len(request_body)
                    request_body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        request_truncated = True
                elif chunk:
                    request_truncated = True
            return message

        async def logging_send(message: Message) -> None:
            nonlocal response_bytes, response_truncated, response_status, response_headers
            if message.get("type") == "http.response.start":
                response_status = int(message.get("status") or 500)
                response_headers = _headers_dict(list(message.get("headers") or []))
            elif message.get("type") == "http.response.body":
                chunk = message.get("body", b"") or b""
                response_bytes += len(chunk)
                if chunk and len(response_body) < self.body_limit:
                    remaining = self.body_limit - len(response_body)
                    response_body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        response_truncated = True
                elif chunk:
                    response_truncated = True
            await send(message)

        try:
            await self.app(scope, logging_receive, logging_send)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = round((time.perf_counter() - started_monotonic) * 1000, 2)
            raw_headers = list(scope.get("headers") or [])
            request_headers = _headers_dict(raw_headers)
            path = str(scope.get("path") or "")
            query_string = scope.get("query_string") or b""
            client = scope.get("client") or (None, None)
            request_content_type = request_headers.get("content-type", "")
            response_content_type = response_headers.get("content-type", "")
            state = scope.get("state") or {}
            entry = {
                "id": uuid.uuid4().hex[:12],
                "created_ts": started_ts,
                "started_at": started_at,
                "log_date": log_date,
                "method": str(scope.get("method") or ""),
                "path": path,
                "query": query_string.decode("utf-8", "replace"),
                "query_params": _query_params(query_string),
                "client": client[0],
                "status_code": response_status,
                "duration_ms": duration_ms,
                "error": error,
                "handler_error": state.get("request_log_error"),
                "request": {
                    "headers": request_headers,
                    **_body_payload(
                        bytes(request_body),
                        content_type=request_content_type,
                        total_bytes=request_bytes,
                        truncated=request_truncated,
                    ),
                },
                "response": {
                    "headers": response_headers,
                    **_body_payload(
                        bytes(response_body),
                        content_type=response_content_type,
                        total_bytes=response_bytes,
                        truncated=response_truncated,
                    ),
                },
            }
            try:
                await request_log_store.add(entry)
            except Exception as log_exc:
                logger.warning("request log write failed: error={}", log_exc)


__all__ = ["RequestLogMiddleware", "request_log_store"]
