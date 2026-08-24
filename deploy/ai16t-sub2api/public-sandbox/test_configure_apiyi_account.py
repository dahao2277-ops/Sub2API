from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        self.assertEqual(target.DATABASE_NAME, "ai99t_sub2api")

    def test_runtime_script_does_not_export_bootstrap_password_by_default(self) -> None:
        script = (target.ROOT / "prepare_runtime.sh").read_text()
        self.assertIn('bootstrap_mode="${AI99T_BOOTSTRAP_MODE:-false}"', script)
        self.assertIn('if [ "$bootstrap_mode" = "true" ]; then', script)
        self.assertNotIn('generate_password_secret "$runtime/admin_password"\ngenerate_', script)
        self.assertIn('if [ "$provider_mode" = "apiyi" ]; then', script)
        self.assertIn('"deepseek-chat"', script)
        self.assertIn('"gpt-5.6-luna"', script)
        self.assertNotIn('"gpt-5.6-sol"', script)

    def _run_prepare_runtime(
        self, bootstrap_mode: str
    ) -> tuple[Path, subprocess.CompletedProcess[str]]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sandbox = root / "public-sandbox"
        sandbox.mkdir()
        script = sandbox / "prepare_runtime.sh"
        shutil.copy2(target.ROOT / "prepare_runtime.sh", script)
        runtime = sandbox / ".runtime"
        runtime.mkdir(mode=0o700)
        (runtime / "apiyi_model_config_core.json").write_text(
            json.dumps(
                {
                    "models": [
                        {"model": model}
                        for model in (
                            "gpt-5.6-luna",
                            "gpt-5.6-terra",
                            "gpt-5.6-sol",
                            "deepseek-chat",
                            "gemini-2.5-flash",
                            "kimi-k2.6",
                        )
                    ]
                }
            )
        )
        environment = os.environ.copy()
        environment.update(
            {
                "AI99T_BOOTSTRAP_MODE": bootstrap_mode,
                "AI16T_PROVIDER_MODE": "apiyi",
                "AI99T_SECRET_ROOT": str(root / "secret-provider"),
                "AI99T_DRIFT_DATA_DIR": str(root / "drift"),
                "SUB2API_RELEASE_COMMIT": "a" * 40,
                "AI16T_CORE_RELEASE_COMMIT": "b" * 40,
                "AI16T_CORE_RELEASE_TREE": "c" * 40,
                "AI16T_CORE_SOURCE_DIR": str(root / "unused-core-source"),
            }
        )
        completed = subprocess.run(
            [str(script)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        return runtime, completed

    def test_prepare_runtime_false_has_no_admin_password_and_replaces_models(self) -> None:
        runtime, completed = self._run_prepare_runtime("false")
        sub2api_env = (runtime / "sub2api.env").read_text().splitlines()
        self.assertFalse(any(line.startswith("ADMIN_PASSWORD=") for line in sub2api_env))
        self.assertFalse((runtime / "admin_password").exists())
        self.assertIn("AI99T_BOOTSTRAP_MODE=false", (runtime / "public.env").read_text())
        models = json.loads((runtime / "apiyi_model_config_core.json").read_text())
        self.assertEqual(
            [item["model"] for item in models["models"]],
            ["deepseek-chat", "gpt-5.6-luna"],
        )
        self.assertNotIn("ADMIN_PASSWORD", completed.stdout + completed.stderr)

    def test_prepare_runtime_true_requires_generated_mode_0600_password(self) -> None:
        runtime, completed = self._run_prepare_runtime("true")
        password_file = runtime / "admin_password"
        password = password_file.read_text().strip()
        self.assertTrue(password)
        self.assertEqual(password_file.stat().st_mode & 0o777, 0o600)
        self.assertIn("ADMIN_PASSWORD=" + password, (runtime / "sub2api.env").read_text())
        self.assertIn("AI99T_BOOTSTRAP_MODE=true", (runtime / "public.env").read_text())
        self.assertNotIn(password, completed.stdout + completed.stderr)

    @mock.patch.object(target, "compose")
    def test_configure_keeps_plaintext_out_and_applies_canary_limits(self, compose: mock.Mock) -> None:
        metadata = {
            "secret_ref": "apiyi/prod-canary",
            "fingerprint": "a" * 64,
            "last4": "1234",
            "created_at": "2026-08-24T00:00:00Z",
            "rotated_at": "2026-08-24T00:00:00Z",
        }

        target.configure(metadata, "a" * 64)

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
        self.assertIn("('default_balance','0')", sql)
        self.assertIn("('default_subscriptions','[]')", sql)
        self.assertIn("('auth_source_default_email_grant_on_signup','true')", sql)
        self.assertIn("('auth_source_default_email_grant_on_first_bind','false')", sql)
        for source in ("linuxdo", "oidc", "wechat", "github", "google", "dingtalk"):
            self.assertIn(f"('auth_source_default_{source}_balance','0')", sql)
            self.assertIn(f"('auth_source_default_{source}_concurrency','1')", sql)
            self.assertIn(f"('auth_source_default_{source}_subscriptions','[]')", sql)
            self.assertIn(f"('auth_source_default_{source}_grant_on_signup','false')", sql)
            self.assertIn(f"('auth_source_default_{source}_grant_on_first_bind','false')", sql)
        self.assertIn("allow_ungrouped_key_scheduling", sql)
        self.assertIn("('payment_enabled','false')", sql)
        self.assertIn("('BALANCE_PAYMENT_DISABLED','true')", sql)
        self.assertIn("('ENABLED_PAYMENT_TYPES','')", sql)
        self.assertIn("payment_provider_instances", sql)
        self.assertIn("'ai99t-internal-' || 'canary-20260824'", sql)
        self.assertIn("COUNT(*) FROM api_keys all_keys", sql)
        self.assertIn("u.role <> 'admin'", sql)
        self.assertNotIn("APIYI-CANARY-OPENAI", sql)

    def test_configure_rejects_unapproved_fingerprint(self) -> None:
        metadata = {
            "secret_ref": "apiyi/prod-canary",
            "fingerprint": "a" * 64,
            "last4": "1234",
            "created_at": "2026-08-24T00:00:00Z",
            "rotated_at": "2026-08-24T00:00:00Z",
        }
        with self.assertRaises(RuntimeError):
            target.configure(metadata, "b" * 64)

    def test_fingerprint_pin_requires_exact_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pin = Path(temporary) / "key-b.fingerprint"
            pin.write_text("a" * 64 + "\n")
            os.chmod(pin, 0o600)
            with mock.patch.dict(
                os.environ, {"AI99T_APIYI_KEY_B_FINGERPRINT_FILE": str(pin)}
            ):
                self.assertEqual(target.load_expected_fingerprint(), "a" * 64)
            os.chmod(pin, 0o644)
            with mock.patch.dict(
                os.environ, {"AI99T_APIYI_KEY_B_FINGERPRINT_FILE": str(pin)}
            ):
                with self.assertRaises(RuntimeError):
                    target.load_expected_fingerprint()


if __name__ == "__main__":
    unittest.main()
