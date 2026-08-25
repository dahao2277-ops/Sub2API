from __future__ import annotations

import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from token_platform.config import Settings
    from token_platform.ledger import LostLease
    from token_platform.models import Usage
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

        dispatches = 0
        dispatch_lock = threading.Lock()
        blue_call = self.blue.gateway.call
        green_call = self.green.gateway.call

        def counted_blue(**kwargs):
            nonlocal dispatches
            with dispatch_lock:
                dispatches += 1
            return blue_call(**kwargs)

        def counted_green(**kwargs):
            nonlocal dispatches
            with dispatch_lock:
                dispatches += 1
            return green_call(**kwargs)

        self.blue.gateway.call = counted_blue
        self.green.gateway.call = counted_green
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.call, self.blue, "cross-slot-replay")
            second = executor.submit(self.call, self.green, "cross-slot-replay")
            original = first.result()
            replay = second.result()
        self.assertEqual(original, replay)
        self.assertEqual(dispatches, 1)

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

    def test_blue_recovers_expired_green_processing_owner_without_double_settlement(
        self,
    ) -> None:
        with self.blue.database.read() as connection:
            before = connection.execute(
                "SELECT available_micro,reserved_micro FROM balances WHERE user_id=?",
                (self.user,),
            ).fetchone()
        self.assertIsNotNone(before)
        self.assertEqual(int(before[1]), 0)

        preauth = self.green.begin_request(
            user_id=self.user,
            api_key_id=self.key,
            provider="openai",
            model="gpt-4o-mini",
            prompt="green processing crash",
            request_id="bg-green-crash",
            idempotency_key="bg-green-crash",
        )
        old_owner = "green-expired-owner"
        self.assertTrue(
            self.green.ledger.claim(preauth.request_id, old_owner, time.time(), 0.05)
        )
        route = self.green._eligible_routes(preauth.request_id)[0]
        self.green.ledger.record_attempt_started(
            request_id=preauth.request_id,
            attempt_number=1,
            supplier_id=route.supplier_id,
            route_id=route.route_id,
            supplier_cost_version_id=route.supplier_cost_version_id,
            owner_token=old_owner,
        )
        with self.green.database.read() as connection:
            processing = connection.execute(
                "SELECT status FROM requests WHERE request_id=?", (preauth.request_id,)
            ).fetchone()
            reserved = connection.execute(
                "SELECT reserved_micro FROM balances WHERE user_id=?", (self.user,)
            ).fetchone()
        self.assertEqual(str(processing[0]), "PROCESSING")
        self.assertGreater(int(reserved[0]), 0)

        time.sleep(0.07)
        self.assertEqual(
            self.blue.recover_incomplete_requests(), [preauth.request_id]
        )
        settlement = self.blue._existing_settlement(preauth.request_id)
        self.assertEqual(settlement.status, "RECONCILIATION_REQUIRED")
        with self.blue.database.read() as connection:
            after = connection.execute(
                "SELECT available_micro,reserved_micro FROM balances WHERE user_id=?",
                (self.user,),
            ).fetchone()
            debits = int(
                connection.execute(
                    "SELECT COUNT(*) FROM ledger_entries WHERE request_id=? AND transaction_type='DEBIT_SETTLEMENT'",
                    (preauth.request_id,),
                ).fetchone()[0]
            )
        self.assertEqual(int(after[1]), 0)
        self.assertEqual(int(after[0]), int(before[0]))
        self.assertEqual(debits, 0)
        with self.assertRaises(LostLease):
            self.green.ledger.record_attempt_finished(
                request_id=preauth.request_id,
                attempt_number=1,
                owner_token=old_owner,
                status="SUCCESS",
                error_code=None,
                latency_ms=1,
                usage_source="provider_reported",
                usage=Usage(1, 1),
            )


if __name__ == "__main__":
    unittest.main()
