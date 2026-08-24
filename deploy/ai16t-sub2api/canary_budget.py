from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Any


MAX_CANARY_INPUT_BYTES = 4096
MAX_CANARY_INPUT_TOKENS = 4096
MAX_CANARY_OUTPUT_TOKENS = 4096


SAFE_TERMINAL_STATUSES = frozenset(
    {"COMPLETED", "PARTIAL_SETTLED", "FAILED_RELEASED", "REFUNDED"}
)
UNRESOLVED_STATUSES = frozenset(
    {"PREAUTHORIZED", "PROCESSING", "RECONCILIATION_REQUIRED", "FINANCIAL_ANOMALY"}
)
POLICY_EPOCH_ADVANCE_ACK = "ADVANCE_AFTER_AUDITED_MARGIN_REMEDIATION"
POLICY_EPOCH_ADVANCE_REASON = "MICRO_ROUNDING_MARGIN_REMEDIATED_V1"
REMEDIABLE_POLICY_ERRORS = frozenset({"actual_dimension_margin_below_minimum"})
FIRST_CUSTOMER_MIN_MARGIN_PPM = 200_000


class CanaryBudgetExceeded(PermissionError):
    """Typed fail-closed Canary admission failure."""


class BudgetReservation:
    def __init__(
        self,
        gate: ProviderSpendGate,
        request_id: str,
        owner_token: str | None,
    ) -> None:
        self._gate = gate
        self.request_id = request_id
        self._owner_token = owner_token
        self._finalized = False

    @property
    def reserved(self) -> bool:
        return self._owner_token is not None

    def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._owner_token is not None:
            self._gate._finalize(self.request_id, self._owner_token)

    def cancel_before_dispatch(self) -> None:
        """Release only a reservation whose provider execution never started."""
        if self._finalized:
            return
        self._finalized = True
        if self._owner_token is not None:
            self._gate._cancel_before_dispatch(self.request_id, self._owner_token)

    def __enter__(self) -> BudgetReservation:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.finalize()


class ProviderSpendGate:
    """Durable, Ledger-coupled provider and customer budget reservation gate.

    Every new provider attempt reserves spend in the same SQLite authority as
    the request Ledger under BEGIN IMMEDIATE. A crash, process restart, or
    unresolved financial state leaves the reservation ACTIVE and therefore
    fail-closes future admissions. Only a safe terminal Ledger state releases
    it; Redis remains a non-authoritative ingress mirror.
    """

    def __init__(
        self,
        database: Any,
        *,
        limit_micro: int,
        daily_limit_micro: int,
        reserve_micro: int,
    ) -> None:
        if (
            limit_micro != 1_000_000
            or daily_limit_micro != 1_000_000
            or reserve_micro != 250_000
        ):
            raise RuntimeError(
                "first-customer budgets must be US$1 total, US$1 daily, "
                "and US$0.25 reserve"
            )
        self.database = database
        self.limit_micro = limit_micro
        self.daily_limit_micro = daily_limit_micro
        self.reserve_micro = reserve_micro
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS canary_budget_reservations(
                request_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                provider_reserved_micro INTEGER NOT NULL CHECK(provider_reserved_micro > 0),
                customer_reserved_micro INTEGER NOT NULL CHECK(customer_reserved_micro > 0),
                owner_token TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE','RELEASED')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                released_at TEXT)"""
            )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_canary_budget_active_user
                ON canary_budget_reservations(user_id,status)"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS canary_budget_policy_state(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                activated_at TEXT NOT NULL,
                request_id_watermark INTEGER NOT NULL CHECK(request_id_watermark >= 0))"""
            )
            # The Key B / first-customer budget is a new policy epoch. Existing
            # APIYI audit records remain immutable and continue to count in the
            # Ledger, but pre-epoch test anomalies must not permanently prevent
            # this independently capped Canary from starting. INSERT OR IGNORE
            # makes the activation watermark durable across process restarts.
            connection.execute(
                """INSERT OR IGNORE INTO canary_budget_policy_state(
                singleton,activated_at,request_id_watermark)
                SELECT 1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),COALESCE(MAX(id),0)
                FROM requests"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS canary_budget_policy_advancements(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                old_watermark INTEGER NOT NULL CHECK(old_watermark >= 0),
                new_watermark INTEGER NOT NULL CHECK(new_watermark > old_watermark),
                request_id TEXT NOT NULL UNIQUE,
                evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
                reason TEXT NOT NULL,
                acknowledgement TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""
            )
            connection.execute(
                """CREATE TRIGGER IF NOT EXISTS canary_policy_advancements_no_update
                BEFORE UPDATE ON canary_budget_policy_advancements
                BEGIN
                  SELECT RAISE(ABORT,'canary policy advancement audit is immutable');
                END"""
            )
            connection.execute(
                """CREATE TRIGGER IF NOT EXISTS canary_policy_advancements_no_delete
                BEFORE DELETE ON canary_budget_policy_advancements
                BEGIN
                  SELECT RAISE(ABORT,'canary policy advancement audit is immutable');
                END"""
            )

    @staticmethod
    def _day_start_utc() -> str:
        malaysia = timezone(timedelta(hours=8), name="Asia/Kuala_Lumpur")
        day_start = datetime.now(timezone.utc).astimezone(malaysia).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return day_start.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _value(row: Any) -> int:
        return int(row["value"] if row else 0)

    def admit(self, request_id: str, user_id: int) -> BudgetReservation:
        if not request_id or user_id < 1:
            raise ValueError("authoritative request ID and user ID are required")
        owner_token = secrets.token_hex(32)
        with self.database.transaction() as connection:
            policy = connection.execute(
                """SELECT activated_at,request_id_watermark
                FROM canary_budget_policy_state
                WHERE singleton=1"""
            ).fetchone()
            if (
                policy is None
                or not str(policy["activated_at"])
                or not isinstance(policy["request_id_watermark"], int)
                or int(policy["request_id_watermark"]) < 0
            ):
                raise CanaryBudgetExceeded("first-customer policy activation is unavailable")
            request_id_watermark = int(policy["request_id_watermark"])
            safe_placeholders = ",".join("?" for _ in SAFE_TERMINAL_STATUSES)
            connection.execute(
                f"""UPDATE canary_budget_reservations AS reservations
                SET status='RELEASED',released_at=(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE status='ACTIVE' AND EXISTS(
                    SELECT 1 FROM requests
                    WHERE requests.request_id=reservations.request_id
                      AND requests.status IN ({safe_placeholders}))""",
                tuple(sorted(SAFE_TERMINAL_STATUSES)),
            )

            request = connection.execute(
                "SELECT status FROM requests WHERE request_id=? LIMIT 1", (request_id,)
            ).fetchone()
            if request is not None:
                status = str(request["status"])
                if status in SAFE_TERMINAL_STATUSES:
                    return BudgetReservation(self, request_id, None)
                raise CanaryBudgetExceeded(
                    f"authoritative request is unresolved: {status}"
                )

            existing = connection.execute(
                """SELECT status FROM canary_budget_reservations
                WHERE request_id=? LIMIT 1""",
                (request_id,),
            ).fetchone()
            if existing is not None:
                raise CanaryBudgetExceeded("authoritative request already has a reservation")

            unresolved_placeholders = ",".join("?" for _ in UNRESOLVED_STATUSES)
            orphan = connection.execute(
                f"""SELECT request_id FROM requests
                WHERE provider='apiyi' AND status IN ({unresolved_placeholders})
                  AND id > ?
                  AND NOT EXISTS(
                    SELECT 1 FROM canary_budget_reservations reservations
                    WHERE reservations.request_id=requests.request_id
                      AND reservations.status='ACTIVE')
                LIMIT 1""",
                (*tuple(sorted(UNRESOLVED_STATUSES)), request_id_watermark),
            ).fetchone()
            if orphan is not None:
                raise CanaryBudgetExceeded("unreserved unresolved provider request exists")

            provider_total = self._value(
                connection.execute(
                    """SELECT COALESCE(SUM(CASE WHEN supplier_total_cost > 0
                    THEN supplier_total_cost ELSE 0 END),0) value FROM requests
                    WHERE provider='apiyi'
                      AND id > ?""",
                    (request_id_watermark,),
                ).fetchone()
            )
            provider_reserved = self._value(
                connection.execute(
                    """SELECT COALESCE(SUM(provider_reserved_micro),0) value
                    FROM canary_budget_reservations WHERE status='ACTIVE'"""
                ).fetchone()
            )
            daily_total = self._value(
                connection.execute(
                    """SELECT COALESCE(SUM(CASE WHEN customer_total_charge > 0
                    THEN customer_total_charge ELSE 0 END),0) value FROM requests
                    WHERE provider='apiyi' AND user_id=?
                      AND id > ?
                      AND julianday(created_at) >= julianday(?)""",
                    (user_id, request_id_watermark, self._day_start_utc()),
                ).fetchone()
            )
            daily_reserved = self._value(
                connection.execute(
                    """SELECT COALESCE(SUM(customer_reserved_micro),0) value
                    FROM canary_budget_reservations
                    WHERE status='ACTIVE' AND user_id=?""",
                    (user_id,),
                ).fetchone()
            )
            if provider_total + provider_reserved + self.reserve_micro > self.limit_micro:
                raise CanaryBudgetExceeded("first-customer upstream spend limit reached")
            if daily_total + daily_reserved + self.reserve_micro > self.daily_limit_micro:
                raise CanaryBudgetExceeded("first-customer daily spend limit reached")

            connection.execute(
                """INSERT INTO canary_budget_reservations(
                request_id,user_id,provider_reserved_micro,customer_reserved_micro,
                owner_token,status) VALUES (?,?,?,?,?,'ACTIVE')""",
                (
                    request_id,
                    user_id,
                    self.reserve_micro,
                    self.reserve_micro,
                    owner_token,
                ),
            )
        return BudgetReservation(self, request_id, owner_token)

    def _finalize(self, request_id: str, owner_token: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM requests WHERE request_id=? LIMIT 1", (request_id,)
            ).fetchone()
            if row is None or str(row["status"]) not in SAFE_TERMINAL_STATUSES:
                return
            connection.execute(
                """UPDATE canary_budget_reservations
                SET status='RELEASED',released_at=(strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                WHERE request_id=? AND owner_token=? AND status='ACTIVE'""",
                (request_id, owner_token),
            )

    def _cancel_before_dispatch(self, request_id: str, owner_token: str) -> None:
        with self.database.transaction() as connection:
            request = connection.execute(
                "SELECT 1 FROM requests WHERE request_id=? LIMIT 1", (request_id,)
            ).fetchone()
            if request is not None:
                return
            connection.execute(
                """DELETE FROM canary_budget_reservations
                WHERE request_id=? AND owner_token=? AND status='ACTIVE'""",
                (request_id, owner_token),
            )


def _json_object(raw: Any, label: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"invalid {label} JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label} JSON object")
    return value


def _token_charge(tokens: int, rate_micro_per_million: int) -> int:
    if tokens < 0 or rate_micro_per_million < 0:
        raise RuntimeError("negative immutable pricing evidence")
    return (tokens * rate_micro_per_million + 999_999) // 1_000_000


def _margin_ppm(revenue_micro: int, cost_micro: int) -> int:
    if revenue_micro <= 0:
        return 0
    return ((revenue_micro - cost_micro) * 1_000_000) // revenue_micro


def _one(rows: list[Any], label: str) -> Any:
    if len(rows) != 1:
        raise RuntimeError(f"exactly one {label} is required")
    return rows[0]


def _policy_advance_evidence(
    connection: Any,
    *,
    expected_current_watermark: int,
    audited_request_id: str,
) -> tuple[int, str]:
    """Return an audit digest only for the one known old-rounding anomaly shape."""
    state = connection.execute(
        """SELECT request_id_watermark FROM canary_budget_policy_state
        WHERE singleton=1"""
    ).fetchone()
    if state is None or int(state["request_id_watermark"]) != expected_current_watermark:
        raise RuntimeError("Canary policy watermark changed")

    request = _one(
        connection.execute(
            """SELECT id,request_id,provider,model,status,error_code,financial_alert,
            supplier_id,price_version_id,supplier_cost_version_id,input_tokens,output_tokens,
            cached_tokens,total_tokens,supplier_total_cost,customer_total_charge,gross_profit,
            gross_margin_ppm,profit_status,fallback_count FROM requests
            WHERE id>? ORDER BY id""",
            (expected_current_watermark,),
        ).fetchall(),
        "post-epoch request",
    )
    if (
        str(request["request_id"]) != audited_request_id
        or str(request["provider"]) != "apiyi"
        or str(request["status"]) != "FINANCIAL_ANOMALY"
        or str(request["error_code"]) not in REMEDIABLE_POLICY_ERRORS
        or int(request["financial_alert"]) != 1
        or int(request["supplier_total_cost"]) <= 0
        or int(request["customer_total_charge"]) != 0
        or int(request["gross_profit"]) != -int(request["supplier_total_cost"])
        or int(request["gross_margin_ppm"]) != 0
        or str(request["profit_status"]) != "FINAL"
        or int(request["fallback_count"]) != 0
    ):
        raise RuntimeError("post-epoch request is not the audited rounding anomaly")

    alert = _one(
        connection.execute(
            """SELECT id,request_id,code,metadata_json FROM financial_alerts
            WHERE request_id=?""",
            (audited_request_id,),
        ).fetchall(),
        "financial alert",
    )
    settlement = _one(
        connection.execute(
            """SELECT id,request_id,transaction_type,amount_micro,input_tokens,output_tokens,
            cached_tokens,total_tokens,cost_micro,revenue_micro,profit_micro,gross_margin_ppm,
            price_version_id,supplier_cost_version_id,status,metadata_json FROM ledger_entries
            WHERE request_id=? AND transaction_type='DEBIT_SETTLEMENT'""",
            (audited_request_id,),
        ).fetchall(),
        "debit settlement",
    )
    attempt = _one(
        connection.execute(
            """SELECT id,request_id,attempt_number,supplier_id,supplier_cost_version_id,
            status,error_code,usage_source,input_tokens,output_tokens,cached_tokens,cost_micro,
            billing_status FROM provider_attempts WHERE request_id=?""",
            (audited_request_id,),
        ).fetchall(),
        "provider attempt",
    )
    attempt_cost = _one(
        connection.execute(
            """SELECT id,request_id,attempt_id,transaction_type,amount_micro,input_tokens,
            output_tokens,cached_tokens,total_tokens,cost_micro,supplier_cost_version_id,status,
            metadata_json FROM ledger_entries WHERE request_id=?
            AND transaction_type='ATTEMPT_COST'""",
            (audited_request_id,),
        ).fetchall(),
        "attempt cost entry",
    )
    if connection.execute(
        """SELECT 1 FROM ledger_entries WHERE request_id=?
        AND transaction_type='COST_ADJUSTMENT' LIMIT 1""",
        (audited_request_id,),
    ).fetchone() is not None:
        raise RuntimeError("cost-adjusted requests are not eligible for policy advancement")

    price = _one(
        connection.execute(
            """SELECT id,provider,model,input_per_million_micro,output_per_million_micro,
            cached_per_million_micro,currency FROM price_versions WHERE id=?""",
            (int(request["price_version_id"]),),
        ).fetchall(),
        "immutable price version",
    )
    supplier = _one(
        connection.execute(
            """SELECT id,supplier_id,provider,model,input_per_million_micro,
            output_per_million_micro,cached_per_million_micro,currency
            FROM supplier_cost_versions WHERE id=?""",
            (int(request["supplier_cost_version_id"]),),
        ).fetchall(),
        "immutable supplier cost version",
    )

    usage = (
        int(request["input_tokens"]),
        int(request["output_tokens"]),
        int(request["cached_tokens"]),
    )
    if usage[0] < usage[2] or any(value < 0 for value in usage):
        raise RuntimeError("invalid immutable usage evidence")
    if int(request["total_tokens"]) != usage[0] + usage[1]:
        raise RuntimeError("request usage total does not match")
    customer_rates = (
        int(price["input_per_million_micro"]),
        int(price["output_per_million_micro"]),
        int(price["cached_per_million_micro"]),
    )
    supplier_rates = (
        int(supplier["input_per_million_micro"]),
        int(supplier["output_per_million_micro"]),
        int(supplier["cached_per_million_micro"]),
    )
    dimensions = (
        ("input", usage[0] - usage[2], customer_rates[0], supplier_rates[0]),
        ("output", usage[1], customer_rates[1], supplier_rates[1]),
        ("cached", usage[2], customer_rates[2], supplier_rates[2]),
    )
    base_revenue = 0
    supplier_cost = 0
    repaired_revenue = 0
    old_failed_dimensions: list[str] = []
    repaired_dimensions: list[str] = []
    for name, tokens, customer_rate, supplier_rate in dimensions:
        customer_charge = _token_charge(tokens, customer_rate)
        supplier_charge = _token_charge(tokens, supplier_rate)
        base_revenue += customer_charge
        supplier_cost += supplier_charge
        repaired_charge = customer_charge
        rate_margin_ok = (
            customer_rate > 0
            and supplier_rate <= customer_rate
            and _margin_ppm(customer_rate, supplier_rate)
            >= FIRST_CUSTOMER_MIN_MARGIN_PPM
        )
        if tokens and not rate_margin_ok:
            raise RuntimeError("immutable rates do not satisfy the minimum margin")
        if tokens and (
            supplier_charge > customer_charge
            or _margin_ppm(customer_charge, supplier_charge)
            < FIRST_CUSTOMER_MIN_MARGIN_PPM
        ):
            old_failed_dimensions.append(name)
        if tokens and supplier_charge:
            denominator = 1_000_000 - FIRST_CUSTOMER_MIN_MARGIN_PPM
            minimum_revenue = (
                supplier_charge * 1_000_000 + denominator - 1
            ) // denominator
            repaired_charge = max(customer_charge, minimum_revenue)
            if repaired_charge > customer_charge:
                repaired_dimensions.append(name)
        repaired_revenue += repaired_charge

    alert_metadata = _json_object(alert["metadata_json"], "financial alert metadata")
    settlement_metadata = _json_object(
        settlement["metadata_json"], "settlement metadata"
    )
    attempt_cost_metadata = _json_object(
        attempt_cost["metadata_json"], "attempt cost metadata"
    )
    expected_usage = {"input": usage[0], "output": usage[1], "cached": usage[2]}
    cost = int(request["supplier_total_cost"])
    if (
        str(alert["request_id"]) != audited_request_id
        or str(alert["code"]) != str(request["error_code"])
        or int(alert_metadata.get("revenue_micro", -1)) != 0
        or int(alert_metadata.get("candidate_revenue_micro", -1)) != base_revenue
        or int(alert_metadata.get("cost_micro", -1)) != cost
        or int(alert_metadata.get("margin_ppm", -1)) != 0
        or alert_metadata.get("failed_dimensions") != old_failed_dimensions
        or not old_failed_dimensions
        or supplier_cost != cost
        or repaired_revenue <= base_revenue
        or _margin_ppm(repaired_revenue, supplier_cost)
        < FIRST_CUSTOMER_MIN_MARGIN_PPM
    ):
        raise RuntimeError("financial alert does not prove the old rounding anomaly")
    if (
        str(settlement["request_id"]) != audited_request_id
        or str(settlement["transaction_type"]) != "DEBIT_SETTLEMENT"
        or str(settlement["status"]) != "FINANCIAL_ANOMALY"
        or int(settlement["amount_micro"]) != 0
        or int(settlement["cost_micro"]) != cost
        or int(settlement["revenue_micro"]) != 0
        or int(settlement["profit_micro"]) != -cost
        or int(settlement["gross_margin_ppm"]) != 0
        or int(settlement["price_version_id"]) != int(request["price_version_id"])
        or int(settlement["supplier_cost_version_id"])
        != int(request["supplier_cost_version_id"])
        or tuple(int(settlement[field]) for field in ("input_tokens", "output_tokens", "cached_tokens"))
        != usage
        or int(settlement["total_tokens"]) != usage[0] + usage[1]
        or settlement_metadata.get("actual_usage") != expected_usage
        or int(settlement_metadata.get("attempt_count", -1)) != 1
        or settlement_metadata.get("failed_margin_dimensions") != old_failed_dimensions
        or int(settlement_metadata.get("candidate_revenue_micro", -1)) != base_revenue
    ):
        raise RuntimeError("debit settlement does not match the audited anomaly")
    if (
        str(attempt["request_id"]) != audited_request_id
        or int(attempt["attempt_number"]) != 1
        or str(attempt["supplier_id"]) != str(request["supplier_id"])
        or int(attempt["supplier_cost_version_id"])
        != int(request["supplier_cost_version_id"])
        or str(attempt["status"]) != "SUCCESS"
        or attempt["error_code"] is not None
        or str(attempt["usage_source"]) != "provider_reported"
        or str(attempt["billing_status"]) != "BILLED"
        or tuple(int(attempt[field]) for field in ("input_tokens", "output_tokens", "cached_tokens"))
        != usage
        or int(attempt["cost_micro"]) != cost
    ):
        raise RuntimeError("provider attempt does not match the audited anomaly")
    if (
        str(attempt_cost["request_id"]) != audited_request_id
        or int(attempt_cost["attempt_id"]) != int(attempt["id"])
        or str(attempt_cost["transaction_type"]) != "ATTEMPT_COST"
        or str(attempt_cost["status"]) != "POSTED"
        or int(attempt_cost["amount_micro"]) != cost
        or int(attempt_cost["cost_micro"]) != cost
        or int(attempt_cost["supplier_cost_version_id"])
        != int(request["supplier_cost_version_id"])
        or tuple(int(attempt_cost[field]) for field in ("input_tokens", "output_tokens", "cached_tokens"))
        != usage
        or int(attempt_cost["total_tokens"]) != usage[0] + usage[1]
        or attempt_cost_metadata.get("cost_semantics") != "final"
        or attempt_cost_metadata.get("usage_complete") is not True
        or attempt_cost_metadata.get("usage_source") != "provider_reported"
    ):
        raise RuntimeError("attempt cost entry does not match the audited anomaly")
    if (
        str(price["provider"]) != str(request["provider"])
        or str(price["model"]) != str(request["model"])
        or str(price["currency"]) != "USD"
        or str(supplier["supplier_id"]) != str(request["supplier_id"])
        or str(supplier["provider"]) != str(request["provider"])
        or str(supplier["model"]) != str(request["model"])
        or str(supplier["currency"]) != "USD"
    ):
        raise RuntimeError("immutable price identity does not match the request")

    evidence = {
        "policy": {
            "old_watermark": expected_current_watermark,
            "minimum_margin_ppm": FIRST_CUSTOMER_MIN_MARGIN_PPM,
            "reason": POLICY_EPOCH_ADVANCE_REASON,
        },
        "request": {key: request[key] for key in request.keys()},
        "alert": {key: alert[key] for key in alert.keys() if key != "metadata_json"},
        "alert_metadata": alert_metadata,
        "settlement": {
            key: settlement[key]
            for key in settlement.keys()
            if key != "metadata_json"
        },
        "settlement_metadata": settlement_metadata,
        "attempt": {key: attempt[key] for key in attempt.keys()},
        "attempt_cost": {
            key: attempt_cost[key]
            for key in attempt_cost.keys()
            if key != "metadata_json"
        },
        "attempt_cost_metadata": attempt_cost_metadata,
        "price": {key: price[key] for key in price.keys()},
        "supplier_cost": {key: supplier[key] for key in supplier.keys()},
        "recomputation": {
            "base_revenue_micro": base_revenue,
            "supplier_cost_micro": supplier_cost,
            "old_failed_dimensions": old_failed_dimensions,
            "repaired_revenue_micro": repaired_revenue,
            "repaired_dimensions": repaired_dimensions,
            "repaired_margin_ppm": _margin_ppm(repaired_revenue, supplier_cost),
        },
    }
    canonical = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return int(request["id"]), hashlib.sha256(canonical).hexdigest()


def canary_policy_audit_sha256(
    database: Any,
    *,
    expected_current_watermark: int,
    audited_request_id: str,
) -> str:
    """Read-only derivation of the exact immutable evidence digest."""
    if expected_current_watermark < 0 or not audited_request_id:
        raise ValueError("expected watermark and audited request ID are required")
    with database.read() as connection:
        return _policy_advance_evidence(
            connection,
            expected_current_watermark=expected_current_watermark,
            audited_request_id=audited_request_id,
        )[1]


def advance_canary_policy_epoch(
    database: Any,
    *,
    expected_current_watermark: int,
    audited_request_id: str,
    expected_evidence_sha256: str,
    acknowledgement: str,
) -> int:
    """Atomically audit, release, append evidence, and advance one policy epoch."""
    if acknowledgement != POLICY_EPOCH_ADVANCE_ACK:
        raise PermissionError("explicit policy epoch acknowledgement is required")
    if expected_current_watermark < 0 or not audited_request_id:
        raise ValueError("expected watermark and audited request ID are required")
    if re.fullmatch(r"[0-9a-f]{64}", expected_evidence_sha256) is None:
        raise ValueError("a lowercase SHA-256 evidence digest is required")

    with database.transaction() as connection:
        request_row_id, actual_evidence_sha256 = _policy_advance_evidence(
            connection,
            expected_current_watermark=expected_current_watermark,
            audited_request_id=audited_request_id,
        )
        if not hmac.compare_digest(expected_evidence_sha256, actual_evidence_sha256):
            raise RuntimeError("Canary policy evidence digest changed")
        active = connection.execute(
            """SELECT request_id FROM canary_budget_reservations
            WHERE status='ACTIVE' ORDER BY request_id"""
        ).fetchall()
        if len(active) != 1 or str(active[0]["request_id"]) != audited_request_id:
            raise RuntimeError("exactly one matching active Canary reservation is required")
        released = connection.execute(
            """UPDATE canary_budget_reservations
            SET status='RELEASED',released_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE request_id=? AND status='ACTIVE'""",
            (audited_request_id,),
        )
        if released.rowcount != 1:
            raise RuntimeError("Canary reservation release failed")
        connection.execute(
            """INSERT INTO canary_budget_policy_advancements(
            old_watermark,new_watermark,request_id,evidence_sha256,reason,acknowledgement)
            VALUES (?,?,?,?,?,?)""",
            (
                expected_current_watermark,
                request_row_id,
                audited_request_id,
                actual_evidence_sha256,
                POLICY_EPOCH_ADVANCE_REASON,
                acknowledgement,
            ),
        )
        changed = connection.execute(
            """UPDATE canary_budget_policy_state
            SET activated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),request_id_watermark=?
            WHERE singleton=1 AND request_id_watermark=?""",
            (request_row_id, expected_current_watermark),
        )
        if changed.rowcount != 1:
            raise RuntimeError("Canary policy watermark compare-and-set failed")
        return request_row_id


class StreamBudgetSession:
    """Own a pre-header reservation until a stream has actually started.

    Closing before the first ``next`` is the only path that may safely cancel
    without a Ledger request: provider dispatch is structurally impossible
    before the wrapped iterator starts. Once started, ordinary fail-closed
    reservation finalization applies.
    """

    def __init__(self, iterator: Any, reservation: BudgetReservation | Any) -> None:
        self._iterator = iterator
        self._reservation = reservation
        self._started = False
        self._closed = False

    def __iter__(self) -> StreamBudgetSession:
        return self

    def __next__(self) -> Any:
        self._started = True
        return next(self._iterator)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            close = getattr(self._iterator, "close", None)
            if close is not None:
                close()
            return
        cancel = getattr(self._reservation, "cancel_before_dispatch", None)
        if cancel is not None:
            cancel()

    def __enter__(self) -> StreamBudgetSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()


def enforce_canary_input_ceiling(prompt: str) -> None:
    """Conservatively bind actual input to the Ledger's token declaration.

    A UTF-8 byte cannot encode more than one token, so a 4096-byte serialized
    request is always at or below the declared 4096-token input ceiling.
    """
    if len(prompt.encode("utf-8")) > MAX_CANARY_INPUT_BYTES:
        raise ValueError("Canary input exceeds the 4096-byte safety ceiling")


def maximum_request_provider_cost_micro(
    input_rate_per_million_micro: int,
    output_rate_per_million_micro: int,
    cached_rate_per_million_micro: int,
    *,
    max_input_tokens: int = MAX_CANARY_INPUT_TOKENS,
    max_output_tokens: int = MAX_CANARY_OUTPUT_TOKENS,
) -> int:
    if min(
        input_rate_per_million_micro,
        output_rate_per_million_micro,
        cached_rate_per_million_micro,
        max_input_tokens,
        max_output_tokens,
    ) < 0:
        raise ValueError("rates and token ceilings must be non-negative")
    numerator = max_input_tokens * max(
        input_rate_per_million_micro, cached_rate_per_million_micro
    ) + max_output_tokens * output_rate_per_million_micro
    return (numerator + 999_999) // 1_000_000
