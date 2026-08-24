import unittest

from app.products._routing import RoutingSession


class RoutingSessionTests(unittest.TestCase):
    def test_account_retry_and_model_fallback_are_distinct_attempts(self):
        routing = {
            "virtual_model": "FREE",
            "resolved_model": "model-1",
            "route": "console",
            "model_pool": "stable",
            "fallback_candidate_pools": {"model-2": "degraded"},
        }
        session = RoutingSession.from_model(
            "model-1",
            candidates=("model-2",),
            routing=routing,
            fallback_budget=1,
        )

        session.begin_attempt("model-1", token="token-1", mode_id=5, pool_id=0)
        session.record_account_retry(status=403, reason="account_auth_failed")
        session.begin_attempt("model-1", token="token-2", mode_id=5, pool_id=0)
        self.assertEqual(
            session.next_model_fallback(status=429),
            "model-2",
        )
        session.begin_attempt("model-2", token="token-3", mode_id=5, pool_id=0)
        session.record_success()

        attempts = routing["route_attempts"]
        self.assertEqual(
            [attempt["outcome"] for attempt in attempts],
            ["account_retry", "model_fallback", "success"],
        )
        self.assertEqual(attempts[0]["upstream_status"], 403)
        self.assertEqual(attempts[1]["model_pool"], "stable")
        self.assertEqual(attempts[2]["model_pool"], "degraded")
        self.assertEqual(routing["fallback_count"], 1)

    def test_stream_started_does_not_change_model(self):
        routing = {"model": "model-1", "route": "grok"}
        session = RoutingSession.from_model(
            "model-1",
            candidates=("model-2",),
            routing=routing,
            fallback_budget=1,
        )
        session.begin_attempt("model-1", token="token-1")

        self.assertIsNone(session.next_model_fallback(status=429, stream_started=True))
        self.assertEqual(session.current_model, "model-1")
        self.assertEqual(routing["route_attempts"][0]["outcome"], "pending")


if __name__ == "__main__":
    unittest.main()
