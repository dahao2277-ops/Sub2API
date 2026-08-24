from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from canary_budget import (
    CanaryBudgetExceeded,
    MAX_CANARY_INPUT_BYTES,
    ProviderSpendGate,
    StreamBudgetSession,
    enforce_canary_input_ceiling,
    maximum_request_provider_cost_micro,
)


class _Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.transaction() as connection:
            connection.execute(
                """CREATE TABLE requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                provider TEXT NOT NULL DEFAULT 'apiyi',
                status TEXT NOT NULL,
                supplier_total_cost INTEGER NOT NULL DEFAULT 0,
                customer_total_charge INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def read(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class ProviderSpendGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = _Database(Path(self.temporary.name) / "ledger.sqlite3")
        self.gate = self._gate()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _gate(self) -> ProviderSpendGate:
        return ProviderSpendGate(
            self.database,
            limit_micro=1_000_000,
            daily_limit_micro=1_000_000,
            reserve_micro=250_000,
        )

    def _insert_request(
        self,
        request_id: str,
        *,
        user_id: int = 11,
        status: str = "COMPLETED",
        provider_cost: int = 0,
        customer_charge: int = 0,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO requests(
                request_id,user_id,status,supplier_total_cost,customer_total_charge
                ) VALUES (?,?,?,?,?)""",
                (request_id, user_id, status, provider_cost, customer_charge),
            )

    def _reservation_status(self, request_id: str) -> str:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT status FROM canary_budget_reservations
                WHERE request_id=?""",
                (request_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return str(row["status"])

    def _reservation_count(self, request_id: str) -> int:
        with self.database.read() as connection:
            row = connection.execute(
                """SELECT COUNT(*) value FROM canary_budget_reservations
                WHERE request_id=?""",
                (request_id,),
            ).fetchone()
        return int(row["value"])

    def test_four_durable_reservations_survive_restart_and_fifth_is_blocked(self) -> None:
        reservations = [self.gate.admit(f"request-{index}", 11) for index in range(4)]
        self.assertTrue(all(reservation.reserved for reservation in reservations))

        restarted_gate = self._gate()
        with self.assertRaises(CanaryBudgetExceeded):
            restarted_gate.admit("request-5", 11)

    def test_concurrent_instances_cannot_overbook_last_reserve(self) -> None:
        self._insert_request("spent", provider_cost=750_000, customer_charge=750_000)
        gates = [self._gate(), self._gate()]
        outcomes: list[str] = []
        lock = threading.Lock()

        def run(index: int) -> None:
            try:
                gates[index].admit(f"race-{index}", 11)
                outcome = "admitted"
            except CanaryBudgetExceeded:
                outcome = "blocked"
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["admitted", "blocked"])

    def test_only_safe_terminal_ledger_state_releases_reservation(self) -> None:
        unresolved = self.gate.admit("processing", 11)
        self._insert_request("processing", status="RECONCILIATION_REQUIRED")
        unresolved.finalize()
        self.assertEqual(self._reservation_status("processing"), "ACTIVE")

        terminal = self.gate.admit("terminal", 12)
        self._insert_request("terminal", user_id=12, status="COMPLETED")
        terminal.finalize()
        self.assertEqual(self._reservation_status("terminal"), "RELEASED")

    def test_settled_replay_does_not_create_new_reservation(self) -> None:
        self._insert_request(
            "settled", provider_cost=1_000_000, customer_charge=1_000_000
        )
        replay = self.gate.admit("settled", 11)
        self.assertFalse(replay.reserved)
        with self.database.read() as connection:
            count = connection.execute(
                "SELECT COUNT(*) value FROM canary_budget_reservations"
            ).fetchone()
        self.assertEqual(int(count["value"]), 0)

    def test_unreserved_unresolved_legacy_request_fails_closed(self) -> None:
        self._insert_request("legacy", status="PROCESSING")
        with self.assertRaises(CanaryBudgetExceeded):
            self.gate.admit("new", 11)

    def test_pre_activation_anomaly_is_preserved_without_blocking_new_policy_epoch(self) -> None:
        database = _Database(Path(self.temporary.name) / "historical-ledger.sqlite3")
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO requests(
                request_id,user_id,status,supplier_total_cost,customer_total_charge,created_at
                ) VALUES (?,?,?,?,?,?)""",
                (
                    "historical-anomaly",
                    11,
                    "FINANCIAL_ANOMALY",
                    900_000,
                    0,
                    "2026-08-24T01:00:00.000Z",
                ),
            )

        gate = ProviderSpendGate(
            database,
            limit_micro=1_000_000,
            daily_limit_micro=1_000_000,
            reserve_micro=250_000,
        )
        reservation = gate.admit("first-key-b-request", 11)
        self.assertTrue(reservation.reserved)
        with database.read() as connection:
            historical = connection.execute(
                "SELECT status FROM requests WHERE request_id=?",
                ("historical-anomaly",),
            ).fetchone()
            activation = connection.execute(
                """SELECT activated_at,request_id_watermark
                FROM canary_budget_policy_state WHERE singleton=1"""
            ).fetchone()
        self.assertEqual(str(historical["status"]), "FINANCIAL_ANOMALY")
        self.assertTrue(str(activation["activated_at"]))
        self.assertGreaterEqual(int(activation["request_id_watermark"]), 1)

        restarted = ProviderSpendGate(
            database,
            limit_micro=1_000_000,
            daily_limit_micro=1_000_000,
            reserve_micro=250_000,
        )
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO requests(
                request_id,user_id,status,supplier_total_cost,customer_total_charge,created_at
                ) VALUES (?,?,?,?,?,?)""",
                (
                    "new-anomaly",
                    11,
                    "FINANCIAL_ANOMALY",
                    1,
                    0,
                    "2000-01-01T00:00:00.000Z",
                ),
            )
        with self.assertRaises(CanaryBudgetExceeded):
            restarted.admit("blocked-after-new-anomaly", 11)

    def test_old_owner_cannot_release_reservation(self) -> None:
        reservation = self.gate.admit("owned", 11)
        self._insert_request("owned", status="COMPLETED")
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE canary_budget_reservations SET owner_token='new-owner'
                WHERE request_id='owned'"""
            )
        reservation.finalize()
        self.assertEqual(self._reservation_status("owned"), "ACTIVE")

    def test_header_failure_before_first_next_releases_without_dispatch(self) -> None:
        reservation = self.gate.admit("header-failure", 11)
        provider_calls: list[str] = []

        def provider_stream():
            provider_calls.append("dispatched")
            yield ("chunk", b"never")

        session = StreamBudgetSession(provider_stream(), reservation)
        with self.assertRaises(BrokenPipeError):
            with session:
                raise BrokenPipeError("client disconnected while writing headers")

        self.assertEqual(provider_calls, [])
        self.assertEqual(self._reservation_count("header-failure"), 0)
        retried = self.gate.admit("header-failure", 11)
        self.assertTrue(retried.reserved)
        self.assertEqual(self._reservation_count("header-failure"), 1)
        self.assertEqual(self._reservation_status("header-failure"), "ACTIVE")

    def test_started_stream_without_terminal_request_remains_fail_closed(self) -> None:
        reservation = self.gate.admit("started", 11)

        def provider_stream():
            yield ("chunk", b"started")

        session = StreamBudgetSession(provider_stream(), reservation)
        self.assertEqual(next(session), ("chunk", b"started"))
        session.close()
        self.assertEqual(self._reservation_status("started"), "ACTIVE")

    def test_daily_customer_charge_is_ledger_backed(self) -> None:
        self._insert_request("daily", customer_charge=800_000)
        with self.assertRaises(CanaryBudgetExceeded):
            self.gate.admit("new-request", 11)

    def test_approved_models_maximum_cost_is_below_reserve(self) -> None:
        self.assertLess(maximum_request_provider_cost_micro(250_000, 1_000_000, 62_500), 250_000)
        self.assertLess(maximum_request_provider_cost_micro(200_000, 1_200_000, 20_000), 250_000)

    def test_actual_input_is_bound_to_declared_token_ceiling(self) -> None:
        enforce_canary_input_ceiling("a" * MAX_CANARY_INPUT_BYTES)
        with self.assertRaises(ValueError):
            enforce_canary_input_ceiling("a" * (MAX_CANARY_INPUT_BYTES + 1))
        with self.assertRaises(ValueError):
            enforce_canary_input_ceiling("汉" * 1366)

    def test_invalid_budget_configuration_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            ProviderSpendGate(
                self.database,
                limit_micro=10_000_000,
                daily_limit_micro=1_000_000,
                reserve_micro=250_000,
            )


if __name__ == "__main__":
    unittest.main()
