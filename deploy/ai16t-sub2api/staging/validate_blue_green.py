from __future__ import annotations

import http.server
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
ACTIVE_SLOT = RUNTIME / "active_slot"
EVIDENCE = RUNTIME / "blue_green_evidence.json"
NGINX_TEMPLATE = ROOT / "nginx.active-slot.conf.template"
SLOTS = {"blue": 18181, "green": 18182}
MAX_BODY = 2 * 1024 * 1024


def switch(slot: str) -> None:
    if slot not in SLOTS:
        raise ValueError("invalid slot")
    temporary = ACTIVE_SLOT.with_suffix(".tmp")
    temporary.write_text(slot + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, ACTIVE_SLOT)


def read_slot() -> str:
    slot = ACTIVE_SLOT.read_text(encoding="utf-8").strip()
    if slot not in SLOTS:
        raise RuntimeError("active slot marker is invalid")
    return slot


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            response.read(MAX_BODY)
            return response.status
    except urllib.error.HTTPError as error:
        error.read(MAX_BODY)
        return error.code


class Proxy(http.server.BaseHTTPRequestHandler):
    def log_message(self, format_string: str, *args: Any) -> None:
        del format_string, args

    def do_GET(self) -> None:
        slot = read_slot()
        upstream = f"http://127.0.0.1:{SLOTS[slot]}{self.path}"
        try:
            with urllib.request.urlopen(upstream, timeout=3) as response:
                body = response.read(MAX_BODY)
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("X-AI16T-Staging-Slot", slot)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except (urllib.error.URLError, TimeoutError):
            self.send_error(503)


def proxy_slot() -> tuple[int, str]:
    request = urllib.request.Request("http://127.0.0.1:18180/ready")
    with urllib.request.urlopen(request, timeout=3) as response:
        response.read(MAX_BODY)
        return response.status, response.headers.get("X-AI16T-Staging-Slot", "")


def main() -> None:
    direct = {
        slot: {
            "health": http_status(f"http://127.0.0.1:{port}/health"),
            "ready": http_status(f"http://127.0.0.1:{port}/ready"),
        }
        for slot, port in SLOTS.items()
    }
    if any(value != 200 for checks in direct.values() for value in checks.values()):
        raise RuntimeError("a staging slot is not healthy and ready")

    switch("blue")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 18180), Proxy)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    transitions: list[dict[str, Any]] = []
    try:
        for requested in ("blue", "green", "blue"):
            switch(requested)
            time.sleep(0.05)
            status, observed = proxy_slot()
            transitions.append(
                {"requested": requested, "observed": observed, "status": status}
            )
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()

    rendered_nginx = NGINX_TEMPLATE.read_text(encoding="utf-8").replace(
        "{{ACTIVE_SLOT_PORT}}", str(SLOTS[read_slot()])
    )
    nginx_output = RUNTIME / "nginx.active.conf"
    nginx_output.write_text(rendered_nginx, encoding="utf-8")
    nginx_output.chmod(0o600)
    if "{{" in rendered_nginx or "listen 127.0.0.1:18180;" not in rendered_nginx:
        raise RuntimeError("Nginx active-slot template render failed")

    passed = all(
        item["requested"] == item["observed"] and item["status"] == 200
        for item in transitions
    )
    evidence = {
        "executed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "direct": direct,
        "transitions": transitions,
        "rollback_target_retained": transitions[-1]["observed"] == "blue",
        "nginx_template_rendered": True,
        "active_slot": read_slot(),
        "result": "PASS" if passed else "FAIL",
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    EVIDENCE.chmod(0o600)
    print(f"BLUE_GREEN={evidence['result']}")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
