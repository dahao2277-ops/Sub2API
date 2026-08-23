from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
ENV_FILE = RUNTIME / "staging.env"
COMPOSE_FILE = ROOT / "compose.staging.yml"
BACKUP_DIR = RUNTIME / "backups"
POSTGRES_USER = "ai16t_sub2api_staging"
PRIMARY_DB = "ai16t_sub2api_staging"
RESTORE_DB = "ai16t_sub2api_restore_verify"


def compose_files() -> list[Path]:
    files = [COMPOSE_FILE]
    override_value = os.environ.get("AI16T_STAGING_COMPOSE_OVERRIDE", "").strip()
    if override_value:
        override = Path(override_value).resolve()
        if override.parent != RUNTIME.resolve() or not override.is_file():
            raise RuntimeError("staging override must be an existing file inside .runtime")
        files.append(override)
    return files


def compose_prefix() -> list[str]:
    command = ["docker", "compose", "--env-file", str(ENV_FILE)]
    for compose_file in compose_files():
        command.extend(["-f", str(compose_file)])
    return command


def compose(*arguments: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*compose_prefix(), *arguments],
        check=True,
        capture_output=capture,
        text=True,
    )


def compose_bytes(*arguments: str) -> bytes:
    completed = subprocess.run(
        [*compose_prefix(), *arguments],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compose_json(output: str) -> list[dict[str, Any]]:
    stripped = output.strip()
    if not stripped:
        return []
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return [json.loads(line) for line in stripped.splitlines() if line.strip()]
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    raise RuntimeError("unexpected Docker Compose JSON output")


def postgres_scalar(database: str, query: str) -> int:
    completed = compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-XAt",
        "-U",
        POSTGRES_USER,
        "-d",
        database,
        "-c",
        query,
        capture=True,
    )
    return int(completed.stdout.strip())


def run() -> dict[str, Any]:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    pg_name = f"postgres-{backup_id}.dump"
    core_name = f"core-ledger-{backup_id}.sqlite3"
    pg_local = BACKUP_DIR / pg_name
    core_local = BACKUP_DIR / core_name

    compose(
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-Fc",
        "-U",
        POSTGRES_USER,
        "-d",
        PRIMARY_DB,
        "-f",
        f"/backup/{pg_name}",
    )
    compose("cp", f"postgres:/backup/{pg_name}", str(pg_local))
    pg_local.chmod(0o600)

    core_backup = compose(
        "exec",
        "-T",
        "commercial-core",
        "python",
        "/staging/core_backup.py",
        "backup",
        "/data/token-platform.sqlite3",
        f"/tmp/{core_name}",
        capture=True,
    )
    core_local.write_bytes(
        compose_bytes(
            "exec",
            "-T",
            "commercial-core",
            "python",
            "-c",
            "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
            f"/tmp/{core_name}",
        )
    )
    core_local.chmod(0o600)
    core_verify = compose(
        "exec",
        "-T",
        "commercial-core",
        "python",
        "/staging/core_backup.py",
        "verify",
        "/data/token-platform.sqlite3",
        f"/tmp/{core_name}",
        capture=True,
    )

    compose("exec", "-T", "postgres", "dropdb", "--if-exists", "-U", POSTGRES_USER, RESTORE_DB)
    compose("exec", "-T", "postgres", "createdb", "-U", POSTGRES_USER, RESTORE_DB)
    compose(
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "-U",
        POSTGRES_USER,
        "-d",
        RESTORE_DB,
        f"/backup/{pg_name}",
    )

    primary_tables = postgres_scalar(
        PRIMARY_DB,
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'",
    )
    restored_tables = postgres_scalar(
        RESTORE_DB,
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'",
    )
    primary_users = postgres_scalar(PRIMARY_DB, "SELECT COUNT(*) FROM users")
    restored_users = postgres_scalar(RESTORE_DB, "SELECT COUNT(*) FROM users")
    if primary_tables != restored_tables or primary_users != restored_users:
        raise RuntimeError("PostgreSQL restore verification mismatch")

    compose(
        "--profile",
        "migration-check",
        "up",
        "-d",
        "--wait",
        "migration-check",
    )
    migration_status = compose_json(compose(
        "--profile",
        "migration-check",
        "ps",
        "--format",
        "json",
        "migration-check",
        capture=True,
    ).stdout)
    compose("--profile", "migration-check", "stop", "migration-check")
    compose("--profile", "migration-check", "rm", "-f", "migration-check")

    # Rollback proof: replace only the disposable verification DB from the
    # immutable backup, never the live staging database.
    compose("exec", "-T", "postgres", "dropdb", "--if-exists", "-U", POSTGRES_USER, RESTORE_DB)
    compose("exec", "-T", "postgres", "createdb", "-U", POSTGRES_USER, RESTORE_DB)
    compose(
        "exec",
        "-T",
        "postgres",
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "-U",
        POSTGRES_USER,
        "-d",
        RESTORE_DB,
        f"/backup/{pg_name}",
    )
    rollback_users = postgres_scalar(RESTORE_DB, "SELECT COUNT(*) FROM users")
    compose("exec", "-T", "postgres", "dropdb", "--if-exists", "-U", POSTGRES_USER, RESTORE_DB)

    evidence = {
        "backup_id": backup_id,
        "postgres_backup": str(pg_local),
        "postgres_sha256": sha256(pg_local),
        "postgres_mode": oct(stat.S_IMODE(pg_local.stat().st_mode)),
        "postgres_primary_tables": primary_tables,
        "postgres_restored_tables": restored_tables,
        "postgres_primary_users": primary_users,
        "postgres_restored_users": restored_users,
        "migration_check_healthy": len(migration_status) == 1
        and migration_status[0].get("State") == "running"
        and migration_status[0].get("Health") == "healthy",
        "rollback_users": rollback_users,
        "core_backup": str(core_local),
        "core_sha256": sha256(core_local),
        "core_mode": oct(stat.S_IMODE(core_local.stat().st_mode)),
        "core_backup_result": json.loads(core_backup.stdout),
        "core_restore_verify": json.loads(core_verify.stdout),
        "result": "PASS",
    }
    if not evidence["migration_check_healthy"] or rollback_users != primary_users:
        raise RuntimeError("migration or rollback verification failed")
    output = RUNTIME / "backup_restore_evidence.json"
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    return evidence


if __name__ == "__main__":
    result = run()
    print(f"BACKUP_RESTORE={result['result']}")
