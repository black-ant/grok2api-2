import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.control.proxy.models import ProxyLease
from app.dataplane.reverse.protocol.xai_console_chat import stream_console_chat
from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_BASE


class _Response:
    status_code = 200
    headers = {}
    content = b""

    async def aiter_lines(self):
        if False:
            yield b""


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return _Response()


class ConsoleTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_console_chat_uses_console_clearance_origin(self):
        lease = ProxyLease(lease_id="lease-console")
        proxy = SimpleNamespace(
            acquire=AsyncMock(return_value=lease),
            feedback=AsyncMock(),
        )

        with (
            patch(
                "app.dataplane.proxy.get_proxy_runtime",
                new=AsyncMock(return_value=proxy),
            ),
            patch(
                "app.dataplane.proxy.adapters.session.ResettableSession",
                return_value=_Session(),
            ),
            patch(
                "app.dataplane.proxy.adapters.session.build_session_kwargs",
                return_value={},
            ),
            patch(
                "app.dataplane.reverse.protocol.xai_console_chat._success_feedback",
                return_value=object(),
            ),
        ):
            async for _event in stream_console_chat("sso-token", {}, timeout_s=1):
                pass

        proxy.acquire.assert_awaited_once_with(clearance_origin=CONSOLE_BASE)
        proxy.feedback.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
