#!/usr/bin/env python3
"""Fail closed when the reviewed Gitleaks inventory drifts.

The checked-in report contains metadata only. Gitleaks is always run with full
redaction, and its temporary reports live in a mode-0700 directory that is
deleted before this process exits.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ALLOWED_CLASSIFICATIONS = {
    "REAL_REVOKED_SECRET",
    "PUBLIC_OAUTH_CLIENT_ID",
    "TEST_FIXTURE",
    "EXAMPLE",
    "DOCUMENTATION",
    "HASH",
    "GENERATED_FILE",
    "FALSE_POSITIVE",
}
BLOCKING_CLASSIFICATIONS = {"REAL_ACTIVE_SECRET", "UNKNOWN"}
REQUIRED_FIELDS = {
    "finding_id",
    "rule_id",
    "path",
    "line",
    "commit",
    "fingerprint",
    "value_fingerprint_sha256",
    "source",
    "classification",
    "owner",
    "action",
    "evidence",
    "line_hash_sha256",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(RuntimeError):
    """A sanitized, user-safe verification failure."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"invalid JSON metadata: {path.name}: {type(error).__name__}") from error


def load_manifest(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise VerificationError("classification manifest must contain a findings array")
    return data


def validate_manifest(data: dict[str, Any], ignore_path: Path) -> collections.Counter[str]:
    findings = data["findings"]
    seen_ids: set[str] = set()
    allowlisted_fingerprints: set[str] = set()
    counts: collections.Counter[str] = collections.Counter()

    for finding in findings:
        if not isinstance(finding, dict):
            raise VerificationError("finding must be an object")
        missing = REQUIRED_FIELDS - finding.keys()
        if missing:
            raise VerificationError(
                f"finding {finding.get('finding_id', '[missing]')} lacks metadata fields"
            )
        if {"secret", "match", "raw", "authorization"} & {key.casefold() for key in finding}:
            raise VerificationError(f"finding {finding['finding_id']} contains forbidden raw fields")

        finding_id = finding["finding_id"]
        source = finding["source"]
        fingerprint = finding["fingerprint"]
        classification = finding["classification"]
        if finding_id in seen_ids:
            raise VerificationError(f"duplicate finding id: {finding_id}")
        seen_ids.add(finding_id)

        if source not in {"current_tree", "full_history"}:
            raise VerificationError(f"unsupported source scope: {finding_id}")
        if classification in BLOCKING_CLASSIFICATIONS:
            raise VerificationError(f"blocking classification remains: {finding_id}")
        if classification not in ALLOWED_CLASSIFICATIONS:
            raise VerificationError(f"unallowlisted classification: {finding_id}")
        if not SHA256_RE.fullmatch(finding["value_fingerprint_sha256"]):
            raise VerificationError(f"invalid value fingerprint: {finding_id}")
        if not SHA256_RE.fullmatch(finding["line_hash_sha256"]):
            raise VerificationError(f"invalid line hash: {finding_id}")
        if not finding["rule_id"] or not finding["path"] or not fingerprint:
            raise VerificationError(f"incomplete exact allowlist metadata: {finding_id}")
        if source == "full_history" and not finding["commit"]:
            raise VerificationError(f"history finding has no commit: {finding_id}")
        if source == "current_tree" and finding["commit"] is not None:
            raise VerificationError(f"current-tree finding unexpectedly has a commit: {finding_id}")

        allowlisted_fingerprints.add(fingerprint)
        counts[classification] += 1
        counts[source] += 1

    configured_lines = [
        line.strip()
        for line in ignore_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    configured = set(configured_lines)
    if len(configured_lines) != len(configured):
        raise VerificationError(".gitleaksignore contains duplicate fingerprint lines")
    if configured != allowlisted_fingerprints:
        raise VerificationError(".gitleaksignore does not exactly match the reviewed fingerprint set")

    expected = data.get("summary", {})
    if counts["current_tree"] != expected.get("current_tree"):
        raise VerificationError("current-tree summary count drift")
    if counts["full_history"] != expected.get("full_history"):
        raise VerificationError("full-history summary count drift")
    if expected.get("real_active_secret") != 0 or expected.get("unknown") != 0:
        raise VerificationError("manifest summary contains a blocking classification")
    fingerprint_counts = collections.Counter(
        finding["fingerprint"] for finding in findings
    )
    if expected.get("unique_exact_allowlist_fingerprints") != len(fingerprint_counts):
        raise VerificationError("unique allowlist fingerprint summary drift")
    collision_groups = sum(1 for count in fingerprint_counts.values() if count > 1)
    if expected.get("fingerprint_collision_groups") != collision_groups:
        raise VerificationError("fingerprint collision summary drift")
    return counts


def _run_gitleaks(
    executable: Path,
    scan_root: Path,
    scope: str,
    report_path: Path,
    empty_ignore: Path,
) -> list[dict[str, Any]]:
    if scope == "current_tree":
        command = [
            str(executable),
            "--gitleaks-ignore-path",
            str(empty_ignore),
            "dir",
            "--no-banner",
            "--redact=100",
            "--report-format",
            "json",
            "--report-path",
            str(report_path),
            "--exit-code",
            "0",
            ".",
        ]
    else:
        command = [
            str(executable),
            "--gitleaks-ignore-path",
            str(empty_ignore),
            "detect",
            "--no-banner",
            "--redact=100",
            "--report-format",
            "json",
            "--report-path",
            str(report_path),
            "--exit-code",
            "0",
            "--source",
            ".",
        ]
    result = subprocess.run(
        command,
        cwd=scan_root,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    output = f"{result.stdout}\n{result.stderr}".casefold()
    if result.returncode != 0 or "level=error" in output or "panic:" in output:
        raise VerificationError(f"Gitleaks {scope} scan failed internally")
    report = _read_json(report_path)
    if not isinstance(report, list):
        raise VerificationError(f"Gitleaks {scope} report is not an array")
    return report


def _metadata_key(scope: str, finding: dict[str, Any]) -> tuple[str, str]:
    return scope, str(finding.get("Fingerprint", ""))


def _live_line_hash(repository: Path, scope: str, finding: dict[str, Any]) -> str:
    if scope == "current_tree":
        content = (repository / finding["path"]).read_bytes()
    else:
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(repository),
                "show",
                f"{finding['commit']}:{finding['path']}",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise VerificationError(f"cannot resolve history source for {finding['finding_id']}")
        content = result.stdout
    lines = content.splitlines(keepends=True)
    start = max(0, int(finding["line"]["start"]) - 1)
    end = min(len(lines), int(finding["line"]["end"]))
    return hashlib.sha256(b"".join(lines[start:end])).hexdigest()


def verify_live_scan(
    data: dict[str, Any], repository: Path, executable: Path
) -> collections.Counter[str]:
    expected: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for finding in data["findings"]:
        expected[(finding["source"], finding["fingerprint"])].append(finding)
    actual: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    with tempfile.TemporaryDirectory(prefix="sub2api-gitleaks-") as temp_name:
        temp = Path(temp_name)
        temp.chmod(0o700)
        empty_ignore = temp / "empty.ignore"
        empty_ignore.write_text("# intentionally empty\n", encoding="utf-8")
        empty_ignore.chmod(0o600)

        files_result = subprocess.run(
            ["/usr/bin/git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=repository,
            capture_output=True,
            check=False,
        )
        if files_result.returncode != 0:
            raise VerificationError("Git worktree enumeration failed")
        current_root = temp / "current"
        current_root.mkdir(mode=0o700)
        for raw_name in files_result.stdout.split(b"\0"):
            if not raw_name:
                continue
            try:
                relative = Path(raw_name.decode("utf-8"))
            except UnicodeDecodeError as error:
                raise VerificationError("non-UTF-8 repository path") from error
            if relative.is_absolute() or ".." in relative.parts:
                raise VerificationError("unsafe repository path")
            if relative == Path(".gitleaksignore"):
                continue
            source = repository / relative
            if source.is_symlink():
                raise VerificationError("repository symlink requires review")
            if source.is_file():
                destination = current_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        history_root = temp / "history"
        clone = subprocess.run(
            [
                "/usr/bin/git",
                "clone",
                "--mirror",
                "--quiet",
                "--no-local",
                str(repository),
                str(history_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone.returncode != 0:
            raise VerificationError("isolated Git history clone failed")

        for scope, scan_root in (
            ("current_tree", current_root),
            ("full_history", history_root),
        ):
            report_path = temp / f"{scope}.json"
            report = _run_gitleaks(executable, scan_root, scope, report_path, empty_ignore)
            report_path.chmod(0o600)
            for finding in report:
                key = _metadata_key(scope, finding)
                if not key[1]:
                    raise VerificationError(f"Gitleaks {scope} finding lacks fingerprint")
                actual[key].append(finding)

    missing = expected.keys() - actual.keys()
    extra = actual.keys() - expected.keys()
    if missing:
        raise VerificationError(f"reviewed findings missing from scan: count={len(missing)}")
    if extra:
        # Do not print match/secret data. The count is enough to fail closed.
        raise VerificationError(f"unclassified Gitleaks findings: count={len(extra)}")

    counts: collections.Counter[str] = collections.Counter()
    for key, scanned_group in actual.items():
        reviewed_group = expected[key]
        if len(scanned_group) != len(reviewed_group):
            raise VerificationError(f"finding multiplicity drift for fingerprint={key[1]}")
        scanned = sorted(
            scanned_group,
            key=lambda item: (
                item.get("RuleID", ""),
                item.get("File", ""),
                int(item.get("StartLine", 0)),
                int(item.get("EndLine", 0)),
            ),
        )
        reviewed = sorted(
            reviewed_group,
            key=lambda item: (
                item["rule_id"], item["path"], item["line"]["start"], item["line"]["end"]
            ),
        )
        for finding, catalogued in zip(scanned, reviewed, strict=True):
            if finding.get("RuleID") != catalogued["rule_id"]:
                raise VerificationError(f"rule drift for {catalogued['finding_id']}")
            if finding.get("File") != catalogued["path"]:
                raise VerificationError(f"path drift for {catalogued['finding_id']}")
            if int(finding.get("StartLine", 0)) != catalogued["line"]["start"]:
                raise VerificationError(f"line drift for {catalogued['finding_id']}")
            if key[0] == "full_history" and finding.get("Commit") != catalogued["commit"]:
                raise VerificationError(f"commit drift for {catalogued['finding_id']}")
            if _live_line_hash(repository, key[0], catalogued) != catalogued["line_hash_sha256"]:
                raise VerificationError(f"line hash drift for {catalogued['finding_id']}")
            counts[key[0]] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--gitleaks", type=Path, default=Path("gitleaks"))
    parser.add_argument("--skip-live-scan", action="store_true")
    args = parser.parse_args()

    repository = args.repository.resolve()
    manifest = args.manifest or repository / "security" / "gitleaks-classifications.json"
    try:
        data = load_manifest(manifest)
        counts = validate_manifest(data, repository / ".gitleaksignore")
        if not args.skip_live_scan:
            live = verify_live_scan(data, repository, args.gitleaks)
            if live["current_tree"] != counts["current_tree"]:
                raise VerificationError("live current-tree count mismatch")
            if live["full_history"] != counts["full_history"]:
                raise VerificationError("live full-history count mismatch")
    except (OSError, VerificationError) as error:
        print(f"GITLEAKS_CLASSIFICATION_GATE=FAIL reason={error}")
        return 1

    print(
        "GITLEAKS_CLASSIFICATION_GATE=PASS "
        f"current={counts['current_tree']} history={counts['full_history']} "
        "real_active_secret=0 unknown=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
