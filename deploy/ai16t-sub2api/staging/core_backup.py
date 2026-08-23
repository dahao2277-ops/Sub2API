from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def database_counts(path: Path) -> dict[str, int | str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        tables = int(
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        )
        requests = int(connection.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        ledger = int(connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0])
        return {
            "integrity": integrity,
            "tables": tables,
            "requests": requests,
            "ledger_entries": ledger,
        }
    finally:
        connection.close()


def backup(source: Path, destination: Path) -> dict[str, object]:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    destination.chmod(0o600)
    return {"status": "backed_up", "counts": database_counts(destination)}


def verify(source: Path, backup_path: Path) -> dict[str, object]:
    source_counts = database_counts(source)
    backup_counts = database_counts(backup_path)
    return {
        "status": "verified" if source_counts == backup_counts else "mismatch",
        "source": source_counts,
        "backup": backup_counts,
    }


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in {"backup", "verify"}:
        raise SystemExit("usage: core_backup.py <backup|verify> <source> <backup>")
    operation = sys.argv[1]
    source = Path(sys.argv[2])
    backup_path = Path(sys.argv[3])
    result = backup(source, backup_path) if operation == "backup" else verify(source, backup_path)
    print(json.dumps(result, sort_keys=True))
    if result["status"] == "mismatch":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
