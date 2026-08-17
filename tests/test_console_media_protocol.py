import base64
import json
import unittest

from cryptography.hazmat.primitives.asymmetric import ec

from app.dataplane.reverse.protocol.xai_console_dpop import DpopSession, build_proof
from app.dataplane.reverse.protocol.xai_console_media import (
    build_image_edit_payload,
    build_image_generation_payload,
    build_video_generation_payload,
    parse_video_status,
    upstream_media_model,
)
from app.dataplane.reverse.transport.console_media import _trusted_image_url, _trusted_video_url


class ConsoleMediaProtocolTests(unittest.TestCase):
    def test_quality_alias_uses_remote_quality_model(self):
        self.assertEqual(
            upstream_media_model("grok-imagine-image-quality-2.0"),
            "grok-imagine-image-quality",
        )
        payload = build_image_generation_payload(
            model="grok-imagine-image-quality-2.0",
            prompt="a lighthouse",
            count=2,
            response_format="url",
            aspect_ratio="16:9",
            resolution="2k",
            quality="medium",
        )
        self.assertEqual(payload["model"], "grok-imagine-image-quality")
        self.assertEqual(payload["quality"], "medium")

    def test_image_edit_keeps_single_and_multiple_input_shapes(self):
        single = build_image_edit_payload(
            model="grok-imagine-image-2.0",
            prompt="remove the sign",
            image_urls=["data:image/png;base64,AAAA"],
            count=1,
            response_format="url",
        )
        multiple = build_image_edit_payload(
            model="grok-imagine-image-2.0",
            prompt="combine these",
            image_urls=["https://assets.grok.com/a.png", "https://assets.grok.com/b.png"],
            count=2,
            response_format="b64_json",
        )
        self.assertIn("image", single)
        self.assertNotIn("images", single)
        self.assertEqual(len(multiple["images"]), 2)

    def test_video_reference_constraints_are_explicit(self):
        payload = build_video_generation_payload(
            model="grok-imagine-video-1.5",
            prompt="a slow orbit",
            duration=12,
            aspect_ratio="9:16",
            resolution="1080p",
        )
        self.assertEqual(payload["model"], "grok-imagine-video-1.5")
        self.assertEqual(payload["resolution"], "1080p")
        with self.assertRaisesRegex(ValueError, "1080p"):
            build_video_generation_payload(
                model="grok-imagine-video-1.5",
                prompt="reference shot",
                duration=12,
                resolution="1080p",
                reference_urls=["https://assets.grok.com/a.png"],
            )

    def test_video_status_parses_progress_and_completion(self):
        status, progress, url, error = parse_video_status(
            json.dumps(
                {
                    "status": "processing",
                    "progress": 41,
                    "video": {},
                }
            ).encode()
        )
        self.assertEqual((status, progress, url, error), ("processing", 41, None, None))

    def test_dpop_proof_has_expected_method_url_and_binding(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        session = DpopSession(
            access_token="header.payload.signature",
            private_key=private_key,
            public_jwk={
                "kty": "EC",
                "crv": "P-256",
                "x": "x",
                "y": "y",
            },
            expires_at=4_000_000_000,
        )
        token = build_proof(
            session,
            method="GET",
            url="wss://console.x.ai/v1/realtime?model=grok-voice-latest",
        )
        header, claims, signature = token.split(".")
        decoded_header = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        decoded_claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        self.assertEqual(decoded_header["typ"], "dpop+jwt")
        self.assertEqual(decoded_claims["htm"], "GET")
        self.assertEqual(decoded_claims["htu"], "https://console.x.ai/v1/realtime")
        self.assertEqual(len(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))), 64)

    def test_console_asset_urls_reject_untrusted_redirect_targets(self):
        self.assertTrue(_trusted_image_url("https://assets.grok.com/image.png"))
        self.assertTrue(_trusted_video_url("https://assets.grok.com/video.mp4"))
        self.assertFalse(_trusted_image_url("https://127.0.0.1/image.png"))
        self.assertFalse(_trusted_video_url("https://example.com/video.mp4"))


if __name__ == "__main__":
    unittest.main()
