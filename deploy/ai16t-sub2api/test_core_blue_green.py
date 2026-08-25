from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from token_platform.config import Settings
    from token_platform.platform import TokenPlatform

    CORE_RUNTIME_AVAILABLE = True
except ModuleNotFoundError:
    Settings = None  # type: ignore[assignment,misc]
    TokenPlatform = None  # type: ignore[assignment,misc]
    CORE_RUNTIME_AVAILABLE = False


@unittest.skipUnless(
    CORE_RUNTIME_AVAILABLE, "Commercial Core runtime dependencies unavailable"
)
class CoreBlueGreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        database = str(Path(self.temporary.name) / "shared-ledger.sqlite3")
        settings = Settings(
            database_path=database,
            port=8787,
            runtime_mode="isolated-test",
            test_mode=True,
            processing_lease_seconds=2.0,
        )
        self.blue = TokenPlatform(settings)
        self.blue.initialize()
        self.user = self.blue.register_user(
            "blue-green@example.test", "strong test passphrase"
        )
        self.key, _raw = self.blue.create_api_key(self.user)
        self.blue.add_test_credit(self.user, 100_000_000, "blue-green-credit")
        self.green = TokenPlatform(settings)
        self.green.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, platform: TokenPlatform, suffix: str, failure_plan=None):
        return platform.process_request(
            user_id=self.user,
            api_key_id=self.key,
            provider="openai",
            model="gpt-4o-mini",
            prompt="blue green mock verification",
            request_id="bg-" + suffix,
            idempotency_key="bg-idem-" + suffix,
            failure_plan=failure_plan,
        )

    def counts(self) -> tuple[int, int, int, int]:
        with self.blue.database.read() as connection:
            requests = int(
                connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
            )
            settlements = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE transaction_type='DEBIT_SETTLEMENT'"
                ).fetchone()[0]
            )
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM requests WHERE status IN ('PREAUTHORIZED','PROCESSING')"
                ).fetchone()[0]
            )
            duplicates = int(
                connection.execute(
                    "SELECT COUNT(*) FROM (SELECT idempotency_key FROM ledger_entries GROUP BY idempotency_key HAVING COUNT(*)>1)"
                ).fetchone()[0]
            )
        return requests, settlements, active, duplicates

    def test_switch_rollback_and_cross_instance_idempotency(self) -> None:
        blue_first = self.call(self.blue, "blue-normal")
        self.assertEqual(blue_first.status, "COMPLETED")

        with ThreadPoolExecutor(max_workers=2) as executor:
            in_flight_blue = executor.submit(self.call, self.blue, "blue-in-flight")
            green_new = executor.submit(self.call, self.green, "green-new")
            self.assertEqual(green_new.result().status, "COMPLETED")
            self.assertEqual(in_flight_blue.result().status, "COMPLETED")

        original = self.call(self.green, "cross-slot-replay")
        replay = self.call(self.blue, "cross-slot-replay")
        self.assertEqual(original, replay)

        with self.blue.database.read() as connection:
            suppliers = [
                str(row[0])
                for row in connection.execute(
                    "SELECT supplier_id FROM routes WHERE provider='openai' AND model='gpt-4o-mini'"
                )
            ]
        failed = self.call(
            self.green, "green-failure", {name: "timeout" for name in suppliers}
        )
        self.assertEqual(failed.status, "FAILED_RELEASED")
        blue_after_rollback = self.call(self.blue, "blue-after-rollback")
        self.assertEqual(blue_after_rollback.status, "COMPLETED")

        requests, settlements, active, duplicates = self.counts()
        self.assertEqual(requests, 6)
        self.assertEqual(settlements, 5)
        self.assertEqual(active, 0)
        self.assertEqual(duplicates, 0)


if __name__ == "__main__":
    unittest.main()
