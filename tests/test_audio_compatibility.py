import unittest
from unittest.mock import AsyncMock, patch

from app.products.openai.audio import speech


class _Request:
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"model": "grok-voice-latest", "text": "hello"}


class AudioCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_tts_contract_accepts_remote_text_field(self):
        with patch(
            "app.products.openai.audio._with_console_account",
            new=AsyncMock(return_value=(b"audio", "audio/mpeg", None)),
        ) as account_mock:
            response = await speech(_Request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"audio")
        account_mock.assert_awaited_once()
        operation = account_mock.await_args.args[1]
        with patch(
            "app.products.openai.audio.synthesize_speech",
            new=AsyncMock(return_value=(b"audio", "audio/mpeg", None)),
        ):
            await operation("token")

    async def test_tts_voice_transport_uses_console_voice_paths(self):
        from app.dataplane.reverse.transport import console_media

        with patch(
            "app.dataplane.reverse.transport.console_media.request_bytes",
            new=AsyncMock(return_value=(200, {}, b'{"voices":[]}')),
        ) as request_mock:
            result = await console_media.list_tts_voices("token")
            self.assertEqual(result, {"voices": []})
            self.assertTrue(request_mock.await_args.args[2].endswith("/v1/tts/voices"))

            await console_media.get_tts_voice("token", "voice/a")
            self.assertTrue(request_mock.await_args.args[2].endswith("/v1/tts/voices/voice%2Fa"))


if __name__ == "__main__":
    unittest.main()
