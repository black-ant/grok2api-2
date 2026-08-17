import asyncio
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.platform import request_logging
from app.platform.request_logging import RequestLogStore, RequestLogMiddleware
from app.platform.usage_audit import (
    UsageSource,
    build_audit_record,
    extract_usage,
    summarize_audits,
)


class UsageAuditTests(unittest.TestCase):
    def test_extracts_openai_usage(self):
        usage, source = extract_usage(
            {
                "usage": {
                    "prompt_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens": 8,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                    "total_tokens": 20,
                }
            }
        )

        self.assertEqual(source, UsageSource.ESTIMATED)
        self.assertEqual(
            usage,
            {
                "input_tokens": 12,
                "cached_input_tokens": 3,
                "output_tokens": 8,
                "reasoning_tokens": 2,
                "total_tokens": 20,
            },
        )

    def test_extracts_anthropic_stream_usage(self):
        usage, source = extract_usage(
            '\n'.join(
                [
                    'event: message_start',
                    'data: {"message":{"usage":{"input_tokens":17}}}',
                    'event: message_delta',
                    'data: {"usage":{"output_tokens":5}}',
                ]
            )
        )

        self.assertEqual(source, UsageSource.ESTIMATED)
        self.assertEqual(usage["input_tokens"], 17)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 22)

    def test_failed_image_audit_does_not_count_output(self):
        record = build_audit_record(
            request_id="audit-failed-image",
            created_ts=time.time(),
            started_at="2026-08-17T00:00:00Z",
            path="/v1/images/generations",
            status_code=502,
            duration_ms=125.5,
            response_content_type="application/json",
            response_body={"error": {"code": "upstream_error"}},
            response_truncated=False,
            routing={"model": "grok-imagine-1.0"},
            state={"request_log_usage": {"media_output_images": 2}},
        )

        self.assertIsNotNone(record)
        self.assertFalse(record["success"])
        self.assertEqual(record["error_code"], "upstream_error")
        self.assertEqual(record["media_output_images"], 0)

    def test_summary_reports_usage_coverage_and_breakdowns(self):
        now = time.time()
        records = [
            {
                "created_ts": now - 10,
                "operation": "chat",
                "model": "grok-4.6",
                "status_code": 200,
                "success": True,
                "usage_source": "estimated",
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 5,
                "reasoning_tokens": 1,
                "total_tokens": 15,
                "duration_ms": 100,
            },
            {
                "created_ts": now - 20,
                "operation": "video",
                "model": "grok-imagine-video",
                "status_code": 502,
                "success": False,
                "usage_source": "none",
                "media_output_seconds": 0,
                "duration_ms": 50,
            },
        ]

        summary = summarize_audits(records, period="24h", now_ts=now)

        self.assertEqual(summary["usage"]["requests"], 2)
        self.assertEqual(summary["usage"]["successful_requests"], 1)
        self.assertEqual(summary["usage"]["input_tokens"], 10)
        self.assertEqual(summary["coverage"]["estimated_usage_requests"], 1)
        self.assertEqual(summary["coverage"]["missing_usage_requests"], 1)
        self.assertFalse(summary["pricing"]["available"])
        self.assertEqual({item["operation"] for item in summary["by_operation"]}, {"chat", "video"})


class RequestAuditMiddlewareTests(unittest.TestCase):
    def test_middleware_persists_normalized_audit_snapshot(self):
        async def run_case(directory: Path) -> list[dict]:
            original_store = request_logging.request_log_store
            request_logging.request_log_store = RequestLogStore(directory=directory)
            sent: list[dict] = []
            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"content-type", b"application/json")],
                "client": ("test-client", 1234),
                "state": {},
            }

            async def app(inner_scope, receive, send):
                inner_scope["state"]["request_log_routing"] = {
                    "model": "grok-4.6",
                    "resolved_model": "grok-4.6",
                }
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": (
                            b'{"usage":{"prompt_tokens":10,"completion_tokens":4,'
                            b'"total_tokens":14}}'
                        ),
                        "more_body": False,
                    }
                )

            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                sent.append(message)

            try:
                await RequestLogMiddleware(app)(scope, receive, send)
                return await request_logging.request_log_store.list()
            finally:
                request_logging.request_log_store = original_store

        with TemporaryDirectory() as temporary:
            entries = asyncio.run(run_case(Path(temporary)))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["audit"]["operation"], "chat")
        self.assertEqual(entries[0]["audit"]["usage_source"], "estimated")
        self.assertEqual(entries[0]["audit"]["total_tokens"], 14)

class RequestAuditStoreTests(unittest.TestCase):
    def test_audit_page_and_summary_read_only_normalized_records(self):
        with TemporaryDirectory() as temporary:
            store = RequestLogStore(directory=Path(temporary), retention_days=2)
            log_date = store.retained_dates()[0]
            now = time.time()
            store._write_entry_locked(
                {
                    "id": "request-1",
                    "log_date": log_date,
                    "audit": {
                        "request_id": "request-1",
                        "created_ts": now - 5,
                        "operation": "chat",
                        "model": "grok-4.6",
                        "status_code": 200,
                        "success": True,
                        "usage_source": "estimated",
                        "input_tokens": 4,
                        "output_tokens": 3,
                        "total_tokens": 7,
                        "duration_ms": 20,
                    },
                }
            )
            store._write_entry_locked({"id": "raw-only", "log_date": log_date})

            total, items = asyncio.run(
                store.audit_page(
                    limit=10,
                    offset=0,
                    start_ts=now - 60,
                    end_ts=now + 60,
                )
            )
            summary = asyncio.run(store.audit_summary(period="24h"))

        self.assertEqual(total, 1)
        self.assertEqual(items[0]["request_id"], "request-1")
        self.assertEqual(summary["usage"]["requests"], 1)
        self.assertEqual(summary["usage"]["total_tokens"], 7)


if __name__ == "__main__":
    unittest.main()