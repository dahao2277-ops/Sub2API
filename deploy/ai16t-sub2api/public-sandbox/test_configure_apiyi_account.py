from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import configure_apiyi_account as target


class ConfigureAPIYIAccountTests(unittest.TestCase):
    def test_canary_identity_and_models_are_exact(self) -> None:
        self.assertEqual(target.ACCOUNT_NAME, "APIYI-PROD-CANARY")
        self.assertEqual(target.SECRET_REFERENCE, "apiyi/prod-canary")
        self.assertEqual(target.CANARY_MODELS, ("deepseek-chat", "gpt-5.6-luna"))

    @mock.patch.object(target, "compose")
    def test_configure_keeps_plaintext_out_and_applies_canary_limits(self, compose: mock.Mock) -> None:
        metadata = {
            "secret_ref": "apiyi/prod-canary",
            "fingerprint": "a" * 64,
            "last4": "1234",
            "created_at": "2026-08-24T00:00:00Z",
            "rotated_at": "2026-08-24T00:00:00Z",
        }

        target.configure(metadata)

        arguments = compose.call_args.args
        sql = compose.call_args.kwargs["input_text"]
        credentials_arg = next(value for value in arguments if value.startswith("credentials="))
        extra_arg = next(value for value in arguments if value.startswith("extra="))
        credentials = json.loads(credentials_arg.removeprefix("credentials="))
        extra = json.loads(extra_arg.removeprefix("extra="))

        self.assertEqual(credentials["secret_ref"], "apiyi/prod-canary")
        self.assertNotIn("api_key", credentials)
        self.assertEqual(extra["allowed_models"], ["deepseek-chat", "gpt-5.6-luna"])
        self.assertEqual(extra["customer_concurrency"], 1)
        self.assertEqual(extra["rpm_limit"], 10)
        self.assertEqual(extra["retry_limit"], 1)
        self.assertFalse(extra["retry_4xx"])
        self.assertTrue(extra["cooldown_429"])
        self.assertEqual(extra["first_customer_upstream_spend_limit_usd"], 1)
        self.assertIn("APIYI-PROD-CANARY", sql)
        self.assertIn("CANARY-CUSTOMER-01", sql)
        self.assertIn("models_list_config", sql)
        self.assertIn("auth_source_default_email_balance", sql)
        self.assertIn("allow_ungrouped_key_scheduling", sql)
        self.assertNotIn("APIYI-CANARY-OPENAI", sql)


if __name__ == "__main__":
    unittest.main()
