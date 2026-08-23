from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "secret-scan":
        secret_path = Path(os.environ["AI16T_MOCK_PROVIDER_KEY_FILE"])
        secret = secret_path.read_bytes().strip()
        database_path = Path(os.environ.get("TP_DATABASE_PATH", "/data/token-platform.sqlite3"))
        persisted = secret in database_path.read_bytes()
        print(json.dumps({"provider_secret_persisted_in_core_db": persisted}, sort_keys=True))
        return
    if len(sys.argv) != 3 or sys.argv[1] != "user":
        raise SystemExit("usage: core_evidence.py user <external-user-reference>")
    external_user = sys.argv[2]
    database_path = Path(os.environ.get("TP_DATABASE_PATH", "/data/token-platform.sqlite3"))
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    mapping = connection.execute(
        """SELECT core_user_id FROM sub2api_identity_map
        WHERE external_user_reference=? LIMIT 1""",
        (external_user,),
    ).fetchone()
    if not mapping:
        print(json.dumps({"exists": False}, sort_keys=True))
        return
    user_id = int(mapping["core_user_id"])
    balance = connection.execute(
        "SELECT available_micro,reserved_micro FROM balances WHERE user_id=?", (user_id,)
    ).fetchone()
    aggregate = connection.execute(
        """SELECT
        COUNT(*) requests,
        SUM(CASE WHEN status IN ('COMPLETED','PARTIAL_SETTLED','REFUNDED') THEN 1 ELSE 0 END) settled_requests,
        SUM(CASE WHEN status='FAILED_RELEASED' THEN 1 ELSE 0 END) failed_released_requests,
        COALESCE(SUM(customer_total_charge),0) gross_charge_micro,
        COALESCE(SUM(supplier_total_cost),0) provider_cost_micro
        FROM requests WHERE user_id=?""",
        (user_id,),
    ).fetchone()
    ledger = connection.execute(
        """SELECT
        SUM(CASE WHEN transaction_type='DEBIT_SETTLEMENT' THEN 1 ELSE 0 END) settlements,
        SUM(CASE WHEN transaction_type='RELEASE' THEN 1 ELSE 0 END) releases,
        SUM(CASE WHEN transaction_type='REFUND' THEN 1 ELSE 0 END) refunds
        FROM ledger_entries WHERE user_id=?""",
        (user_id,),
    ).fetchone()
    refunds = connection.execute(
        "SELECT COALESCE(SUM(amount_micro),0) value FROM refunds WHERE user_id=?", (user_id,)
    ).fetchone()
    duplicate_settlements = connection.execute(
        """SELECT COUNT(*) value FROM (
        SELECT request_id,COUNT(*) count FROM ledger_entries
        WHERE user_id=? AND transaction_type='DEBIT_SETTLEMENT'
        GROUP BY request_id HAVING COUNT(*) > 1)""",
        (user_id,),
    ).fetchone()
    duplicate_idempotency = connection.execute(
        """SELECT COUNT(*) value FROM (
        SELECT api_key_id,idempotency_key,COUNT(*) count FROM requests
        WHERE user_id=? GROUP BY api_key_id,idempotency_key HAVING COUNT(*) > 1)""",
        (user_id,),
    ).fetchone()
    attempts = connection.execute(
        """SELECT COUNT(*) value FROM provider_attempts pa
        JOIN requests r ON r.request_id=pa.request_id WHERE r.user_id=?""",
        (user_id,),
    ).fetchone()
    result = {
        "exists": True,
        "available_micro": int(balance["available_micro"]),
        "reserved_micro": int(balance["reserved_micro"]),
        "negative_balance": int(balance["available_micro"]) < 0
        or int(balance["reserved_micro"]) < 0,
        "requests": int(aggregate["requests"] or 0),
        "settled_requests": int(aggregate["settled_requests"] or 0),
        "failed_released_requests": int(aggregate["failed_released_requests"] or 0),
        "gross_charge_micro": int(aggregate["gross_charge_micro"] or 0),
        "refund_micro": int(refunds["value"] or 0),
        "net_revenue_micro": int(aggregate["gross_charge_micro"] or 0)
        - int(refunds["value"] or 0),
        "provider_cost_micro": int(aggregate["provider_cost_micro"] or 0),
        "ledger_settlements": int(ledger["settlements"] or 0),
        "ledger_releases": int(ledger["releases"] or 0),
        "ledger_refunds": int(ledger["refunds"] or 0),
        "duplicate_settlements": int(duplicate_settlements["value"]),
        "duplicate_idempotency": int(duplicate_idempotency["value"]),
        "provider_attempts": int(attempts["value"]),
    }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
