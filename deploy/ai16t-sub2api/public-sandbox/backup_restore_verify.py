from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
ENV_FILE = RUNTIME / "public.env"
COMPOSE_FILE = ROOT / "compose.public-sandbox.yml"
configured_backup_dir = os.environ.get("AI99T_BACKUP_DIR", "").strip()
if configured_backup_dir:
    BACKUP_DIR = Path(configured_backup_dir)
    if not BACKUP_DIR.is_absolute():
        raise RuntimeError("AI99T_BACKUP_DIR must be an absolute path")
else:
    BACKUP_DIR = RUNTIME / "backups"
POSTGRES_USER = "ai99t_sub2api"
PRIMARY_DB = "ai99t_sub2api"
RESTORE_DB = "ai99t_sub2api_restore_verify"
SECRET_BACKUP_ROOT = BACKUP_DIR / "secret-provider"


def compose(*arguments: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            *arguments,
        ],
        check=True,
        capture_output=capture,
        text=True,
    )


def compose_bytes(*arguments: str) -> bytes:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            *arguments,
        ],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def compose_optional(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_FILE),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def run() -> dict[str, object]:
    BACKUP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    SECRET_BACKUP_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    pg_name = f"postgres-{backup_id}.dump"
    core_name = f"core-ledger-{backup_id}.sqlite3"
    pg_local = BACKUP_DIR / pg_name
    core_local = BACKUP_DIR / core_name
    secret_backup_dir = SECRET_BACKUP_ROOT / backup_id
    secret_backup_dir.mkdir(mode=0o700)
    secret_local = secret_backup_dir / "secrets.json"

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
        "/ops/core_backup.py",
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
        "/ops/core_backup.py",
        "verify",
        "/data/token-platform.sqlite3",
        f"/tmp/{core_name}",
        capture=True,
    )

    secret_metadata: list[dict[str, object]] = []
    for reference in ("apiyi/integration", "apiyi/prod-canary"):
        secret_probe = compose_optional(
            "exec",
            "-T",
            "secret-provider",
            "/app/ai16t-secret-provider",
            "metadata",
            "--ref",
            reference,
        )
        if secret_probe.returncode == 0:
            item = json.loads(secret_probe.stdout)
            item.pop("value", None)
            secret_metadata.append(item)
    secret_store_probe = compose_optional(
        "exec", "-T", "secret-provider", "test", "-f", "/data/secrets.json"
    )
    if secret_store_probe.returncode == 0:
        compose("cp", "secret-provider:/data/secrets.json", str(secret_local))
        secret_local.chmod(0o600)
        encrypted_payload = secret_local.read_text(encoding="utf-8")
        if "sk-" in encrypted_payload:
            raise RuntimeError("encrypted SecretProvider backup contains a plaintext key marker")
        secret_verify = compose_optional(
            "run",
            "--rm",
            "--no-deps",
            "--user",
            "0:0",
            "-e",
            "AI16T_SECRET_DATA_DIR=/verify-data",
            "-v",
            f"{secret_backup_dir}:/verify-data",
            "secret-provider",
            "verify-store",
        )
        if secret_verify.returncode != 0 or "SECRET_STORE_VERIFY=PASS" not in secret_verify.stdout:
            raise RuntimeError("encrypted SecretProvider backup restore verification failed")

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
    compose("exec", "-T", "postgres", "dropdb", "--if-exists", "-U", POSTGRES_USER, RESTORE_DB)

    result = {
        "backup_id": backup_id,
        "postgres_backup": str(pg_local),
        "postgres_sha256": sha256(pg_local),
        "postgres_mode": oct(stat.S_IMODE(pg_local.stat().st_mode)),
        "postgres_primary_tables": primary_tables,
        "postgres_restored_tables": restored_tables,
        "postgres_primary_users": primary_users,
        "postgres_restored_users": restored_users,
        "core_backup": str(core_local),
        "core_sha256": sha256(core_local),
        "core_mode": oct(stat.S_IMODE(core_local.stat().st_mode)),
        "core_backup_result": json.loads(core_backup.stdout),
        "core_restore_verify": json.loads(core_verify.stdout),
        "secret_provider_backup": str(secret_local) if secret_local.exists() else "",
        "secret_provider_sha256": sha256(secret_local) if secret_local.exists() else "",
        "secret_provider_mode": (
            oct(stat.S_IMODE(secret_local.stat().st_mode)) if secret_local.exists() else ""
        ),
        "secret_provider_metadata": secret_metadata,
        "result": "PASS",
    }
    evidence = RUNTIME / "backup_restore_evidence.json"
    evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    evidence.chmod(0o600)
    return result


if __name__ == "__main__":
    evidence = run()
    print(f"PUBLIC_SANDBOX_BACKUP_RESTORE={evidence['result']}")
