import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.platform.errors import ValidationError


asset_upload = importlib.import_module("app.dataplane.reverse.transport.asset_upload")
video = importlib.import_module("app.products.openai.video")
router_module = importlib.import_module("app.products.openai.router")


class _FakeConfig:
    def __init__(self, *, max_redirects: int = 5, input_max_mb: int = 1) -> None:
        self.max_redirects = max_redirects
        self.input_max_mb = input_max_mb

    def get_float(self, key: str, default: float) -> float:
        return default

    def get_int(self, key: str, default: int) -> int:
        if key == "asset.max_redirects":
            return self.max_redirects
        if key == "asset.input_max_mb":
            return self.input_max_mb
        return default


class _FakeResponse:
    def __init__(self, status_code: int, *, headers=None, chunks=()) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"".join(chunks)
        self._chunks = tuple(chunks)

    def aiter_content(self):
        async def _iterate():
            for chunk in self._chunks:
                yield chunk

        return _iterate()


class _FakeSession:
    responses: list[_FakeResponse] = []

    def __init__(self, **kwargs) -> None:
        del kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return self.responses.pop(0)


class _FakeProxy:
    def __init__(self) -> None:
        self.feedbacks = []

    async def acquire(self):
        return "lease"

    async def feedback(self, lease, feedback):
        self.feedbacks.append((lease, feedback))


class VideoSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_public_host_validation_rejects_private_and_local_hosts(self):
        rejected = (
            "127.0.0.1",
            "::1",
            "10.0.0.1",
            "192.168.1.1",
            "169.254.1.1",
            "fc00::1",
            "localhost",
            "printer.local",
            "service.internal",
        )
        for host in rejected:
            with self.subTest(host=host):
                with self.assertRaises(ValidationError):
                    asset_upload._validate_public_host(host)

        asset_upload._validate_public_host("8.8.8.8")

    def test_hostname_validation_checks_all_resolved_addresses(self):
        with patch.object(
            asset_upload.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.168.1.10", 0))],
        ):
            with self.assertRaises(ValidationError):
                asset_upload._validate_public_host("example.test")

        with patch.object(
            asset_upload.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("8.8.8.8", 0))],
        ):
            asset_upload._validate_public_host("example.test")

    async def test_remote_url_rejects_credentials_and_non_http_schemes(self):
        with self.assertRaises(ValidationError):
            await asset_upload._validate_remote_url("https://user:pass@example.com/a")
        with self.assertRaises(ValidationError):
            await asset_upload._validate_remote_url("ftp://example.com/a")

    def test_data_uri_and_mime_validation(self):
        with self.assertRaises(ValidationError):
            asset_upload.parse_data_uri("data:image/png;base64,not-valid!")

        self.assertEqual(
            asset_upload._validate_mime(
                "application/octet-stream", "reference.png", ("image/",)
            ),
            "image/png",
        )
        with self.assertRaises(ValidationError):
            asset_upload._validate_mime("text/html", "reference.png", ("image/",))

    async def test_limited_response_rejects_stream_over_limit(self):
        response = _FakeResponse(200, chunks=(b"abc", b"d"))
        with self.assertRaises(ValidationError):
            await asset_upload._read_limited_response(response, 3)

        response = _FakeResponse(200, chunks=(b"ab", b"c"))
        self.assertEqual(await asset_upload._read_limited_response(response, 3), b"abc")

    async def test_redirect_limit_is_enforced_and_feedback_is_not_duplicated(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [
            _FakeResponse(302, headers={"location": "/next"}),
            _FakeResponse(302, headers={"location": "/again"}),
        ]

        async def _noop_validate(_url):
            return None

        with (
            patch.object(asset_upload, "get_proxy_runtime", new=AsyncMock(return_value=proxy)),
            patch.object(asset_upload, "ResettableSession", _FakeSession),
            patch.object(asset_upload, "build_http_headers", return_value={}),
            patch.object(asset_upload, "build_session_kwargs", return_value={}),
            patch.object(asset_upload, "_validate_remote_url", new=_noop_validate),
            patch.object(
                asset_upload,
                "get_config",
                side_effect=lambda key=None, default=None: (
                    _FakeConfig(max_redirects=1) if key is None else 1
                ),
            ),
        ):
            with self.assertRaises(ValidationError):
                await asset_upload.upload_from_input("token", "https://example.com/a")

        self.assertEqual(len(proxy.feedbacks), 1)

    async def test_content_length_limit_is_enforced_before_upload(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [
            _FakeResponse(200, headers={"content-length": str(1024 * 1024 + 1)})
        ]

        async def _noop_validate(_url):
            return None

        with (
            patch.object(asset_upload, "get_proxy_runtime", new=AsyncMock(return_value=proxy)),
            patch.object(asset_upload, "ResettableSession", _FakeSession),
            patch.object(asset_upload, "build_http_headers", return_value={}),
            patch.object(asset_upload, "build_session_kwargs", return_value={}),
            patch.object(asset_upload, "_validate_remote_url", new=_noop_validate),
            patch.object(asset_upload, "upload_file", new=AsyncMock()) as upload_mock,
            patch.object(
                asset_upload,
                "get_config",
                side_effect=lambda key=None, default=None: (
                    _FakeConfig(input_max_mb=1) if key is None else 1
                ),
            ),
        ):
            with self.assertRaises(ValidationError):
                await asset_upload.upload_from_input("token", "https://example.com/a")

        upload_mock.assert_not_awaited()

    async def test_same_idempotency_key_reuses_job_and_rejects_different_request(self):
        async with video._VIDEO_JOBS_LOCK:
            video._VIDEO_JOBS.clear()
            video._VIDEO_IDEMPOTENCY.clear()

        first = video._VideoJob(
            id="video_first",
            model="grok-video",
            prompt="a cat",
            seconds="6",
            size="720x1280",
            quality="standard",
            created_at=1,
        )
        second = video._VideoJob(
            id="video_second",
            model="grok-video",
            prompt="a dog",
            seconds="6",
            size="720x1280",
            quality="standard",
            created_at=1,
        )
        signature = "same-request"
        self.assertIsNone(
            await video._put_video_job(
                first, idempotency_key="request-1", request_signature=signature
            )
        )
        self.assertIs(
            await video._put_video_job(
                second, idempotency_key="request-1", request_signature=signature
            ),
            first,
        )
        with self.assertRaises(ValidationError):
            await video._put_video_job(
                second, idempotency_key="request-1", request_signature="different"
            )

        async with video._VIDEO_JOBS_LOCK:
            video._VIDEO_JOBS.clear()
            video._VIDEO_IDEMPOTENCY.clear()

    def test_trusted_video_url_and_safe_filename_rules(self):
        self.assertTrue(video._is_safe_video_url("https://assets.grok.com/a.mp4"))
        self.assertTrue(video._is_safe_video_url("https://grok.com/a.mp4"))
        self.assertFalse(video._is_safe_video_url("http://assets.grok.com/a.mp4"))
        self.assertFalse(video._is_safe_video_url("https://example.com/a.mp4"))
        self.assertFalse(video._is_safe_video_url("https://user@assets.grok.com/a.mp4"))
        self.assertFalse(video._is_safe_video_url("https://[invalid/a.mp4"))
        self.assertEqual(video._safe_video_filename("video_123", "video/webm"), "video_123.webm")
        self.assertEqual(video._safe_video_filename("../../bad", "video/mp4"), "bad.mp4")

    async def test_local_file_endpoint_serves_non_mp4_video(self):
        file_id = "0123456789abcdef"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / f"{file_id}.webm"
            path.write_bytes(b"webm")
            with patch.object(router_module, "video_files_dir", return_value=Path(temp_dir)):
                response = await router_module.serve_video(id=file_id)

        self.assertEqual(response.media_type, "video/webm")


if __name__ == "__main__":
    unittest.main()
