import json
import unittest
from types import SimpleNamespace

import aiohttp

from app.dataplane.reverse.transport.imagine_ws import (
    _frame_request_id,
    _stream_round,
)

_IMAGE_URL = "https://imagine-public.x.ai/imagine-public/images/{}.jpg"


def _text_frame(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(payload))


def _start_stage(image_id: str, **extra) -> SimpleNamespace:
    return _text_frame(
        {
            "type": "json",
            "current_status": "start_stage",
            "image_id": image_id,
            "order": 0,
            "width": 512,
            "height": 768,
            **extra,
        }
    )


def _completed(image_id: str, **extra) -> SimpleNamespace:
    return _text_frame(
        {
            "type": "json",
            "current_status": "completed",
            "image_id": image_id,
            "moderated": False,
            "r_rated": False,
            **extra,
        }
    )


def _image_frame(image_id: str, blob: str, **extra) -> SimpleNamespace:
    return _text_frame(
        {
            "type": "image",
            "url": _IMAGE_URL.format(image_id),
            "blob": blob,
            "percentage_complete": 100,
            **extra,
        }
    )


def _idle_probe() -> SimpleNamespace:
    """Reply to the round-end liveness probe as if no CLOSE frame arrived."""
    return _text_frame({"type": "noop"})


class _ScriptedWS:
    """Minimal WS stand-in driven by a per-step script."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def receive(self):
        step = self._steps.pop(0)
        return step(self)

    @property
    def request_id(self) -> str:
        content = self.sent[1]["item"]["content"][0]
        return str(content["requestId"])


async def _run_round(ws, *, needed: int = 1):
    events = [
        event
        async for event in _stream_round(
            ws,
            "a cat",
            aspect_ratio="2:3",
            enable_nsfw=False,
            enable_pro=False,
            needed=needed,
            stream_timeout_s=0.5,
            round_timeout_s=5.0,
            inter_round_wait_s=0.01,
        )
    ]
    return events


class FrameRequestIdTests(unittest.TestCase):
    def test_reads_top_level_variants(self):
        for key in ("requestId", "request_id", "req_id", "reqId"):
            self.assertEqual(_frame_request_id({key: " img_1 "}), "img_1")

    def test_reads_nested_item_id(self):
        self.assertEqual(_frame_request_id({"item": {"requestId": "img_2"}}), "img_2")
        self.assertEqual(_frame_request_id({"item": {"request_id": "img_3"}}), "img_3")

    def test_missing_id_is_empty(self):
        self.assertEqual(_frame_request_id({}), "")
        self.assertEqual(_frame_request_id({"item": {"other": 1}}), "")
        self.assertEqual(_frame_request_id({"requestId": "   "}), "")


class StreamRoundAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_frames_from_previous_round_are_ignored(self):
        def stale_start(_ws):
            return _start_stage("aaaa1111-0001", requestId="img_previous")

        def stale_image(_ws):
            return _image_frame("aaaa1111-0001", "STALE", requestId="img_previous")

        def stale_completed(_ws):
            return _completed("aaaa1111-0001", requestId="img_previous")

        def valid_start(ws):
            return _start_stage("ffff1111-0001", requestId=ws.request_id)

        def valid_image(ws):
            return _image_frame("ffff1111-0001", "FRESH", requestId=ws.request_id)

        def valid_completed(ws):
            return _completed("ffff1111-0001", requestId=ws.request_id)

        ws = _ScriptedWS(
            [
                stale_start,
                stale_image,
                stale_completed,
                valid_start,
                valid_image,
                valid_completed,
                lambda _ws: _idle_probe(),
            ]
        )
        events = await _run_round(ws)

        finals = [event for event in events if event.get("is_final")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["image_id"], "ffff1111-0001")
        self.assertEqual(finals[0]["blob"], "FRESH")
        self.assertFalse([event for event in events if event["type"] == "error"])
        self.assertEqual(events[-1]["type"], "_meta")

    async def test_idless_frames_stay_supported_before_attribution(self):
        ws = _ScriptedWS(
            [
                lambda _ws: _start_stage("bbbb1111-0001"),
                lambda _ws: _image_frame("bbbb1111-0001", "LEGACY"),
                lambda _ws: _completed("bbbb1111-0001"),
                lambda _ws: _idle_probe(),
            ]
        )
        events = await _run_round(ws)

        finals = [event for event in events if event.get("is_final")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["blob"], "LEGACY")

    async def test_idless_frames_are_dropped_after_attribution(self):
        def valid_start(ws):
            return _start_stage("ffff2222-0002", requestId=ws.request_id)

        def idless_start(_ws):
            return _start_stage("cccc1111-0001")

        def valid_image(ws):
            return _image_frame("ffff2222-0002", "FRESH2", requestId=ws.request_id)

        def valid_completed(ws):
            return _completed("ffff2222-0002", requestId=ws.request_id)

        ws = _ScriptedWS(
            [
                valid_start,
                idless_start,
                valid_image,
                valid_completed,
                lambda _ws: _idle_probe(),
            ]
        )
        events = await _run_round(ws)

        finals = [event for event in events if event.get("is_final")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["image_id"], "ffff2222-0002")
        self.assertFalse(
            [event for event in events if event.get("image_id") == "cccc1111-0001"]
        )


if __name__ == "__main__":
    unittest.main()
