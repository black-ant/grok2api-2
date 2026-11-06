import unittest
from types import SimpleNamespace

from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.proxy.models import ProxyFeedbackKind
from app.dataplane.account import AccountDirectory
from app.dataplane.reverse.classifier import classify_result
from app.dataplane.reverse.feedback import (
    build_account_feedback,
    build_proxy_feedback,
)
from app.dataplane.reverse.protocol.xai_console_chat import _status_feedback
from app.dataplane.reverse.protocol.xai_usage import (
    is_content_refusal_body,
    is_content_refusal_error,
)
from app.dataplane.reverse.transport._proxy_feedback import upstream_feedback
from app.dataplane.reverse.types import ResultCategory, ReverseResult
from app.platform.errors import UpstreamError

REFUSAL_BODY = (
    '{"code":"permission-denied","error":"I\'m sorry, I can\'t help with that request."}'
)
REFUSAL_BODY_CURLY = (
    '{"code":"permission-denied","error":"I\u2019m sorry, I can\u2019t help with that request."}'
)
ACCOUNT_DENIAL_BODY = (
    '{"code":"permission-denied","error":"access to the chat endpoint is denied"}'
)


def _error(status: int, body: str) -> UpstreamError:
    return UpstreamError("upstream failed", status=status, body=body)


class RefusalDetectionTests(unittest.TestCase):
    def test_detects_refusal_bodies(self):
        self.assertTrue(is_content_refusal_body(REFUSAL_BODY))
        self.assertTrue(is_content_refusal_body(REFUSAL_BODY_CURLY))
        self.assertTrue(is_content_refusal_body("Sorry, I cannot help with that request."))
        self.assertTrue(
            is_content_refusal_body("This content violates usage guidelines.")
        )
        self.assertTrue(
            is_content_refusal_body('{"error":"safety_check_type_policy"}')
        )

    def test_ignores_account_and_generic_bodies(self):
        self.assertFalse(is_content_refusal_body(ACCOUNT_DENIAL_BODY))
        self.assertFalse(is_content_refusal_body("invalid-credentials"))
        self.assertFalse(is_content_refusal_body(""))
        self.assertFalse(is_content_refusal_body("rate limit exceeded"))

    def test_error_detection_requires_403(self):
        self.assertTrue(is_content_refusal_error(_error(403, REFUSAL_BODY)))
        self.assertFalse(is_content_refusal_error(_error(400, REFUSAL_BODY)))
        self.assertFalse(is_content_refusal_error(_error(403, ACCOUNT_DENIAL_BODY)))
        self.assertFalse(is_content_refusal_error(RuntimeError(REFUSAL_BODY)))


class RefusalClassificationTests(unittest.TestCase):
    def test_classifier_marks_refusal(self):
        self.assertEqual(
            classify_result(403, REFUSAL_BODY), ResultCategory.REFUSAL
        )

    def test_classifier_keeps_account_denial_as_auth_failure(self):
        self.assertEqual(
            classify_result(403, "invalid-credentials"), ResultCategory.AUTH_FAILURE
        )

    def test_classifier_keeps_other_403_forbidden(self):
        self.assertEqual(
            classify_result(403, ACCOUNT_DENIAL_BODY), ResultCategory.FORBIDDEN
        )

    def test_feedback_kind_for_refusal_is_refused(self):
        self.assertEqual(
            feedback_kind_for_error(_error(403, REFUSAL_BODY)),
            FeedbackKind.REFUSED,
        )

    def test_feedback_kind_for_other_403_stays_forbidden(self):
        self.assertEqual(
            feedback_kind_for_error(_error(403, ACCOUNT_DENIAL_BODY)),
            FeedbackKind.FORBIDDEN,
        )

    def test_refusal_does_not_retry_even_when_403_is_retryable(self):
        from app.products.openai.chat import _should_retry_upstream

        refusal = _error(403, REFUSAL_BODY)
        self.assertFalse(_should_retry_upstream(refusal, frozenset({403})))
        self.assertTrue(
            _should_retry_upstream(_error(403, ACCOUNT_DENIAL_BODY), frozenset({403}))
        )


class RefusalFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_ignores_refused_feedback(self):
        directory = AccountDirectory(repository=None)
        # Only idx_by_token is provided: any penalty would need more table state
        # and would fail loudly instead of silently demoting the account.
        directory._table = SimpleNamespace(idx_by_token={"token-1": 0})

        await directory.feedback("token-1", FeedbackKind.REFUSED, 0)

    def test_proxy_feedback_is_neutral_for_refusal(self):
        feedback = upstream_feedback(_error(403, REFUSAL_BODY))
        self.assertEqual(feedback.kind, ProxyFeedbackKind.NEUTRAL)

    def test_console_status_feedback_is_neutral_for_refusal(self):
        feedback = _status_feedback(403, REFUSAL_BODY)
        self.assertEqual(feedback.kind, ProxyFeedbackKind.NEUTRAL)

    def test_console_status_feedback_keeps_challenge_for_other_403(self):
        feedback = _status_feedback(403, ACCOUNT_DENIAL_BODY)
        self.assertEqual(feedback.kind, ProxyFeedbackKind.CHALLENGE)

    def test_reverse_account_patch_skips_failure_counters(self):
        patch = build_account_feedback(
            "token-1",
            ReverseResult(category=ResultCategory.REFUSAL, status_code=403),
        )
        self.assertIsNone(patch.usage_fail_delta)
        self.assertIsNone(patch.last_fail_at)

    def test_reverse_proxy_feedback_is_neutral_for_refusal(self):
        feedback = build_proxy_feedback(
            ReverseResult(category=ResultCategory.REFUSAL, status_code=403)
        )
        self.assertEqual(feedback.kind, ProxyFeedbackKind.NEUTRAL)


if __name__ == "__main__":
    unittest.main()
