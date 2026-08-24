from __future__ import annotations

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
