import base64
import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.platform.errors import UpstreamError

asset_upload = importlib.import_module("app.dataplane.reverse.transport.asset_upload")

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"") -> None:
        self.status_code = status_code
        self.content = body


class _FakeProxy:
    def __init__(self) -> None:
        self.acquisitions = 0
        self.feedbacks = []

    async def acquire(self, **_kwargs):
        self.acquisitions += 1
        return SimpleNamespace(proxy_url=None, clearance_host="grok.com")

    async def feedback(self, _lease, result) -> None:
        self.feedbacks.append(result)


class _FakeSession:
    responses: list[_FakeResponse] = []
    calls: list[dict] = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class AssetUploadV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        asset_upload._upload_sem = None
        _FakeSession.responses = []
        _FakeSession.calls = []

    def _patch_upload(self, proxy: _FakeProxy, *, form_sink: list | None = None):
        headers = {
            "Content-Type": "application/json",
            "x-xai-request-id": "legacy-request-id",
            "Accept": "*/*",
        }
        header_calls: list[dict] = []

        def _fake_headers(_token, **kwargs):
            header_calls.append(kwargs)
            return dict(headers)

        def _fake_form(filename, mime, raw, file_source):
            if form_sink is not None:
                form_sink.append((filename, mime, raw, file_source))
            return SimpleNamespace(close=lambda: None)

        patches = (
            patch.object(
                asset_upload, "get_proxy_runtime", new=AsyncMock(return_value=proxy)
            ),
            patch.object(asset_upload, "ResettableSession", _FakeSession),
            patch.object(asset_upload, "build_http_headers", side_effect=_fake_headers),
            patch.object(asset_upload, "build_session_kwargs", return_value={}),
            patch.object(asset_upload, "_build_upload_form", side_effect=_fake_form),
        )
        return patches, header_calls

    async def test_upload_posts_multipart_to_v2_endpoint(self):
        proxy = _FakeProxy()
        form_sink: list = []
        _FakeSession.responses = [
            _FakeResponse(
                200,
                b'{"uploadId":"upload-1","fileMetadata":{"fileMetadataId":"meta-1","fileUri":"users/u/meta-1/content"}}',
            )
        ]
        patches, header_calls = self._patch_upload(proxy, form_sink=form_sink)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            file_id, file_uri = await asset_upload.upload_file(
                "token",
                "reference.png",
                "image/png",
                _PNG_B64,
                file_source=asset_upload.FILE_SOURCE_IMAGINE_SELF,
                referer=asset_upload.IMAGINE_REFERER,
            )

        self.assertEqual(file_id, "meta-1")
        self.assertEqual(file_uri, "users/u/meta-1/content")
        self.assertEqual(len(_FakeSession.calls), 1)
        call = _FakeSession.calls[0]
        self.assertEqual(call["url"], "https://grok.com/http/upload-file-v2/direct")
        self.assertIn("multipart", call)
        self.assertNotIn("data", call)
        self.assertNotIn("Content-Type", call["headers"])
        self.assertNotIn("x-xai-request-id", call["headers"])
        self.assertEqual(form_sink[0][3], "IMAGINE_SELF_UPLOAD_FILE_SOURCE")
        self.assertEqual(form_sink[0][2], base64.b64decode(_PNG_B64))
        self.assertEqual(header_calls[0]["referer"], "https://grok.com/imagine")

    async def test_chat_upload_sends_no_file_source(self):
        proxy = _FakeProxy()
        form_sink: list = []
        _FakeSession.responses = [_FakeResponse(200, b'{"uploadId":"upload-3"}')]
        patches, header_calls = self._patch_upload(proxy, form_sink=form_sink)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            await asset_upload.upload_file(
                "token", "photo.png", "image/png", _PNG_B64
            )

        self.assertIsNone(form_sink[0][3])
        self.assertIsNone(header_calls[0]["referer"])


    async def test_upload_retries_once_after_empty_challenge_403(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [
            _FakeResponse(403, b""),
            _FakeResponse(200, b'{"uploadId":"upload-2"}'),
        ]
        patches, _header_calls = self._patch_upload(proxy)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            file_id, file_uri = await asset_upload.upload_file(
                "token",
                "reference.png",
                "image/png",
                _PNG_B64,
            )

        self.assertEqual(file_id, "upload-2")
        self.assertEqual(file_uri, "")
        self.assertEqual(len(_FakeSession.calls), 2)
        self.assertEqual(proxy.acquisitions, 2)

    async def test_structured_403_is_not_replayed(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [
            _FakeResponse(403, b'{"error":{"code":"7","message":"account blocked"}}')
        ]
        patches, _header_calls = self._patch_upload(proxy)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(UpstreamError) as raised:
                await asset_upload.upload_file(
                    "token",
                    "reference.png",
                    "image/png",
                    _PNG_B64,
                )

        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(len(_FakeSession.calls), 1)

    async def test_terminal_error_in_success_body_is_reported(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [
            _FakeResponse(200, b'{"terminalError":{"code":"moderated"}}')
        ]
        patches, _header_calls = self._patch_upload(proxy)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(UpstreamError) as raised:
                await asset_upload.upload_file(
                    "token", "reference.png", "image/png", _PNG_B64
                )

        self.assertIn("rejected upstream", raised.exception.message)
        self.assertIn("moderated", raised.exception.message)
        self.assertEqual(raised.exception.status, 502)

    async def test_terminal_error_string_reason_is_surfaced(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [
            _FakeResponse(
                200,
                b'{"terminalError":"Image dimensions are insufficient"}',
            )
        ]
        patches, _header_calls = self._patch_upload(proxy)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(UpstreamError) as raised:
                await asset_upload.upload_file(
                    "token", "reference.png", "image/png", _PNG_B64
                )

        self.assertIn("dimensions are insufficient", raised.exception.message)
        self.assertEqual(raised.exception.status, 502)

    async def test_missing_file_reference_is_reported(self):
        proxy = _FakeProxy()
        _FakeSession.responses = [_FakeResponse(200, b"{}")]
        patches, _header_calls = self._patch_upload(proxy)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            with self.assertRaises(UpstreamError) as raised:
                await asset_upload.upload_file(
                    "token", "reference.png", "image/png", _PNG_B64
                )

        self.assertIn("no file reference", raised.exception.message)
        self.assertEqual(raised.exception.status, 502)

    def test_upload_form_carries_file_source_part(self):
        form = asset_upload._build_upload_form(
            "reference.png", "image/png", b"png-bytes", asset_upload.FILE_SOURCE_IMAGINE_SELF
        )
        self.addCleanup(form.close)
        self.assertTrue(type(form).__name__ == "CurlMime")
        self.assertEqual(
            asset_upload._safe_filename("bad\x00name\n.png"), "bad_name_.png"
        )
        self.assertEqual(asset_upload._safe_filename("  "), "upload.bin")


if __name__ == "__main__":
    unittest.main()
