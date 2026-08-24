from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from canary_budget import (
    CanaryBudgetExceeded,
    MAX_CANARY_INPUT_BYTES,
    POLICY_EPOCH_ADVANCE_ACK,
    ProviderSpendGate,
    StreamBudgetSession,
    advance_canary_policy_epoch,
    canary_policy_audit_sha256,
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
                model TEXT NOT NULL DEFAULT 'deepseek-chat',
                status TEXT NOT NULL,
                error_code TEXT,
                financial_alert INTEGER NOT NULL DEFAULT 0,
                supplier_id TEXT,
                price_version_id INTEGER,
                supplier_cost_version_id INTEGER,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                supplier_total_cost INTEGER NOT NULL DEFAULT 0,
                customer_total_charge INTEGER NOT NULL DEFAULT 0,
                gross_profit INTEGER NOT NULL DEFAULT 0,
                gross_margin_ppm INTEGER NOT NULL DEFAULT 0,
                profit_status TEXT NOT NULL DEFAULT 'FINAL',
                fallback_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
            )
            connection.execute(
                """CREATE TABLE financial_alerts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,
                code TEXT NOT NULL,metadata_json TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE price_versions(
                id INTEGER PRIMARY KEY,provider TEXT NOT NULL,model TEXT NOT NULL,
                input_per_million_micro INTEGER NOT NULL,
                output_per_million_micro INTEGER NOT NULL,
                cached_per_million_micro INTEGER NOT NULL,currency TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE supplier_cost_versions(
                id INTEGER PRIMARY KEY,supplier_id TEXT NOT NULL,provider TEXT NOT NULL,
                model TEXT NOT NULL,input_per_million_micro INTEGER NOT NULL,
                output_per_million_micro INTEGER NOT NULL,
                cached_per_million_micro INTEGER NOT NULL,currency TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE provider_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,supplier_id TEXT NOT NULL,
                supplier_cost_version_id INTEGER NOT NULL,status TEXT NOT NULL,
                error_code TEXT,usage_source TEXT NOT NULL,input_tokens INTEGER,
                output_tokens INTEGER,cached_tokens INTEGER,cost_micro INTEGER,
                billing_status TEXT NOT NULL)"""
            )
            connection.execute(
                """CREATE TABLE ledger_entries(
                id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,
                attempt_id INTEGER,transaction_type TEXT NOT NULL,amount_micro INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_micro INTEGER NOT NULL DEFAULT 0,revenue_micro INTEGER NOT NULL DEFAULT 0,
                profit_micro INTEGER NOT NULL DEFAULT 0,gross_margin_ppm INTEGER NOT NULL DEFAULT 0,
                price_version_id INTEGER,supplier_cost_version_id INTEGER,status TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}')"""
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
        error_code: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO requests(
                request_id,user_id,status,error_code,supplier_total_cost,customer_total_charge
                ) VALUES (?,?,?,?,?,?)""",
                (request_id, user_id, status, error_code, provider_cost, customer_charge),
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

    def _insert_rounding_anomaly(
        self,
        request_id: str = "audited-margin",
        *,
        customer_input_rate: int = 312_500,
        create_reservation: bool = True,
    ) -> None:
        if create_reservation:
            self.assertTrue(self.gate.admit(request_id, 11).reserved)
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO price_versions VALUES(
                8,'apiyi','deepseek-chat',?,1250000,78125,'USD')""",
                (customer_input_rate,),
            )
            connection.execute(
                """INSERT OR IGNORE INTO supplier_cost_versions VALUES(
                12,'apiyi-canary','apiyi','deepseek-chat',250000,1000000,62500,'USD')"""
            )
            connection.execute(
                """INSERT INTO requests(
                request_id,user_id,provider,model,status,error_code,financial_alert,
                supplier_id,price_version_id,supplier_cost_version_id,input_tokens,
                output_tokens,cached_tokens,total_tokens,supplier_total_cost,
                customer_total_charge,gross_profit,gross_margin_ppm,profit_status,fallback_count)
                VALUES (?,11,'apiyi','deepseek-chat','FINANCIAL_ANOMALY',
                'actual_dimension_margin_below_minimum',1,'apiyi-canary',8,12,
                6,4,0,10,6,0,-6,0,'FINAL',0)""",
                (request_id,),
            )
            connection.execute(
                """INSERT INTO provider_attempts(
                request_id,attempt_number,supplier_id,supplier_cost_version_id,status,error_code,
                usage_source,input_tokens,output_tokens,cached_tokens,cost_micro,billing_status)
                VALUES (?,1,'apiyi-canary',12,'SUCCESS',NULL,'provider_reported',6,4,0,6,'BILLED')""",
                (request_id,),
            )
            attempt_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
            connection.execute(
                """INSERT INTO ledger_entries(
                request_id,attempt_id,transaction_type,amount_micro,input_tokens,output_tokens,
                cached_tokens,total_tokens,cost_micro,supplier_cost_version_id,status,metadata_json)
                VALUES (?,?,'ATTEMPT_COST',6,6,4,0,10,6,12,'POSTED',?)""",
                (
                    request_id,
                    attempt_id,
                    json.dumps(
                        {
                            "cost_semantics": "final",
                            "usage_complete": True,
                            "usage_source": "provider_reported",
                        },
                        sort_keys=True,
                    ),
                ),
            )
            settlement_metadata = {
                "actual_usage": {"input": 6, "output": 4, "cached": 0},
                "attempt_count": 1,
                "candidate_revenue_micro": 7,
                "cost_policy": "immutable-all-attempt-cost-entries",
                "failed_margin_dimensions": ["input"],
            }
            connection.execute(
                """INSERT INTO ledger_entries(
                request_id,transaction_type,amount_micro,input_tokens,output_tokens,cached_tokens,
                total_tokens,cost_micro,revenue_micro,profit_micro,gross_margin_ppm,
                price_version_id,supplier_cost_version_id,status,metadata_json)
                VALUES (?,'DEBIT_SETTLEMENT',0,6,4,0,10,6,0,-6,0,8,12,
                'FINANCIAL_ANOMALY',?)""",
                (request_id, json.dumps(settlement_metadata, sort_keys=True)),
            )
            alert_metadata = {
                "revenue_micro": 0,
                "candidate_revenue_micro": 7,
                "cost_micro": 6,
                "margin_ppm": 0,
                "failed_dimensions": ["input"],
            }
            connection.execute(
                """INSERT INTO financial_alerts(request_id,code,metadata_json)
                VALUES (?,'actual_dimension_margin_below_minimum',?)""",
                (request_id, json.dumps(alert_metadata, sort_keys=True)),
            )

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

    def test_audited_margin_remediation_advances_exactly_one_request(self) -> None:
        self._insert_rounding_anomaly()
        digest = canary_policy_audit_sha256(
            self.database,
            expected_current_watermark=0,
            audited_request_id="audited-margin",
        )

        watermark = advance_canary_policy_epoch(
            self.database,
            expected_current_watermark=0,
            audited_request_id="audited-margin",
            expected_evidence_sha256=digest,
            acknowledgement=POLICY_EPOCH_ADVANCE_ACK,
        )

        self.assertEqual(watermark, 1)
        self.assertEqual(self._reservation_status("audited-margin"), "RELEASED")
        with self.database.read() as connection:
            request = connection.execute(
                "SELECT status,error_code FROM requests WHERE request_id='audited-margin'"
            ).fetchone()
            state = connection.execute(
                """SELECT request_id_watermark FROM canary_budget_policy_state
                WHERE singleton=1"""
            ).fetchone()
            audit = connection.execute(
                """SELECT old_watermark,new_watermark,request_id,evidence_sha256,reason
                FROM canary_budget_policy_advancements"""
            ).fetchone()
        self.assertEqual(str(request["status"]), "FINANCIAL_ANOMALY")
        self.assertEqual(
            str(request["error_code"]), "actual_dimension_margin_below_minimum"
        )
        self.assertEqual(int(state["request_id_watermark"]), 1)
        self.assertEqual(
            tuple(audit),
            (0, 1, "audited-margin", digest, "MICRO_ROUNDING_MARGIN_REMEDIATED_V1"),
        )
        self.assertTrue(self.gate.admit("post-remediation", 11).reserved)

    def test_policy_advance_rejects_unrelated_or_unaudited_state(self) -> None:
        self._insert_rounding_anomaly()
        self._insert_request("unrelated", status="COMPLETED")
        with self.assertRaises(RuntimeError):
            canary_policy_audit_sha256(
                self.database,
                expected_current_watermark=0,
                audited_request_id="audited-margin",
            )
        with self.assertRaises(PermissionError):
            advance_canary_policy_epoch(
                self.database,
                expected_current_watermark=0,
                audited_request_id="audited-margin",
                expected_evidence_sha256="0" * 64,
                acknowledgement="",
            )

    def test_policy_advance_rejects_true_low_rate_even_with_matching_shape(self) -> None:
        self._insert_rounding_anomaly(customer_input_rate=250_000)
        with self.assertRaisesRegex(RuntimeError, "immutable rates"):
            canary_policy_audit_sha256(
                self.database,
                expected_current_watermark=0,
                audited_request_id="audited-margin",
            )

    def test_policy_advance_requires_alert_settlement_and_active_reservation(self) -> None:
        for table in ("financial_alerts", "ledger_entries"):
            database = _Database(Path(self.temporary.name) / f"missing-{table}.sqlite3")
            gate = ProviderSpendGate(
                database,
                limit_micro=1_000_000,
                daily_limit_micro=1_000_000,
                reserve_micro=250_000,
            )
            original_database, original_gate = self.database, self.gate
            self.database, self.gate = database, gate
            try:
                self._insert_rounding_anomaly()
                with database.transaction() as connection:
                    if table == "financial_alerts":
                        connection.execute("DELETE FROM financial_alerts")
                    else:
                        connection.execute(
                            "DELETE FROM ledger_entries WHERE transaction_type='DEBIT_SETTLEMENT'"
                        )
                with self.assertRaises(RuntimeError):
                    canary_policy_audit_sha256(
                        database,
                        expected_current_watermark=0,
                        audited_request_id="audited-margin",
                    )
            finally:
                self.database, self.gate = original_database, original_gate

        database = _Database(Path(self.temporary.name) / "no-active-reservation.sqlite3")
        gate = ProviderSpendGate(
            database,
            limit_micro=1_000_000,
            daily_limit_micro=1_000_000,
            reserve_micro=250_000,
        )
        original_database, original_gate = self.database, self.gate
        self.database, self.gate = database, gate
        try:
            self._insert_rounding_anomaly(create_reservation=False)
            digest = canary_policy_audit_sha256(
                database,
                expected_current_watermark=0,
                audited_request_id="audited-margin",
            )
            with self.assertRaisesRegex(RuntimeError, "exactly one matching active"):
                advance_canary_policy_epoch(
                    database,
                    expected_current_watermark=0,
                    audited_request_id="audited-margin",
                    expected_evidence_sha256=digest,
                    acknowledgement=POLICY_EPOCH_ADVANCE_ACK,
                )
        finally:
            self.database, self.gate = original_database, original_gate

    def test_policy_advance_digest_mismatch_rolls_back_all_changes(self) -> None:
        self._insert_rounding_anomaly()
        with self.assertRaisesRegex(RuntimeError, "digest changed"):
            advance_canary_policy_epoch(
                self.database,
                expected_current_watermark=0,
                audited_request_id="audited-margin",
                expected_evidence_sha256="0" * 64,
                acknowledgement=POLICY_EPOCH_ADVANCE_ACK,
            )
        self.assertEqual(self._reservation_status("audited-margin"), "ACTIVE")
        with self.database.read() as connection:
            watermark = connection.execute(
                "SELECT request_id_watermark FROM canary_budget_policy_state WHERE singleton=1"
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM canary_budget_policy_advancements"
            ).fetchone()[0]
        self.assertEqual(int(watermark), 0)
        self.assertEqual(int(audit_count), 0)

    def test_policy_advance_rolls_back_release_and_audit_when_cas_fails(self) -> None:
        self._insert_rounding_anomaly()
        digest = canary_policy_audit_sha256(
            self.database,
            expected_current_watermark=0,
            audited_request_id="audited-margin",
        )
        with self.database.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER reject_policy_state_update
                BEFORE UPDATE ON canary_budget_policy_state
                BEGIN SELECT RAISE(ABORT,'simulated compare-and-set failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            advance_canary_policy_epoch(
                self.database,
                expected_current_watermark=0,
                audited_request_id="audited-margin",
                expected_evidence_sha256=digest,
                acknowledgement=POLICY_EPOCH_ADVANCE_ACK,
            )
        self.assertEqual(self._reservation_status("audited-margin"), "ACTIVE")
        with self.database.read() as connection:
            watermark = connection.execute(
                "SELECT request_id_watermark FROM canary_budget_policy_state WHERE singleton=1"
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM canary_budget_policy_advancements"
            ).fetchone()[0]
        self.assertEqual(int(watermark), 0)
        self.assertEqual(int(audit_count), 0)

    def test_policy_advancement_audit_is_append_only(self) -> None:
        self._insert_rounding_anomaly()
        digest = canary_policy_audit_sha256(
            self.database,
            expected_current_watermark=0,
            audited_request_id="audited-margin",
        )
        advance_canary_policy_epoch(
            self.database,
            expected_current_watermark=0,
            audited_request_id="audited-margin",
            expected_evidence_sha256=digest,
            acknowledgement=POLICY_EPOCH_ADVANCE_ACK,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            with self.database.transaction() as connection:
                connection.execute("DELETE FROM canary_budget_policy_advancements")

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
