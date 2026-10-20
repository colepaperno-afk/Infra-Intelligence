#!/usr/bin/env python3
"""Serve the editorial dashboard on loopback only; no public deployment endpoint."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Lock
from urllib.parse import urlsplit
import webbrowser

from review import DEFAULT_DECISIONS, DEFAULT_QUEUE, build_dashboard, record_decision


MAX_REQUEST_BYTES = 16 * 1024
DASHBOARD_FILE = Path(__file__).resolve().parent / "review-dashboard.html"


def authorize_write(host: str, origin: str, expected_port: int, token: str, supplied: str) -> bool:
    try:
        parsed_host = urlsplit(f"http://{host}")
        parsed_origin = urlsplit(origin)
    except ValueError:
        return False
    if parsed_host.hostname not in {"127.0.0.1", "localhost"} or parsed_host.port != expected_port:
        return False
    return (
        parsed_origin.scheme == "http"
        and parsed_origin.netloc == host
        and hmac.compare_digest(token, supplied)
    )


def local_reviewer() -> str:
    result = subprocess.run(
        ["git", "config", "user.name"],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    reviewer = result.stdout.strip()
    if result.returncode != 0 or not reviewer:
        raise RuntimeError("Configure a reviewer identity with `git config user.name` before starting the dashboard.")
    return reviewer


def create_server(
    queue_path: Path = DEFAULT_QUEUE,
    decisions_path: Path = DEFAULT_DECISIONS,
    *,
    reviewer: str | None = None,
    token: str | None = None,
    port: int = 8765,
) -> ThreadingHTTPServer:
    reviewer_name = reviewer or local_reviewer()
    csrf_token = token or secrets.token_urlsafe(32)
    script_nonce = secrets.token_urlsafe(18)
    decision_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "InfraReview/1.0"

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; object-src 'none'; form-action 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def _authorized(self, require_origin: bool = False) -> bool:
            if not self._authorized_host():
                return False
            host = self.headers.get("Host", "")
            if require_origin:
                return authorize_write(
                    host,
                    self.headers.get("Origin", ""),
                    self.server.server_address[1],
                    csrf_token,
                    self.headers.get("X-Review-Token", ""),
                )
            return hmac.compare_digest(csrf_token, self.headers.get("X-Review-Token", ""))

        def do_GET(self) -> None:
            if not self._authorized_host():
                self._send(403, b"Local requests only", "text/plain; charset=utf-8")
                return
            if self.path == "/":
                try:
                    document = DASHBOARD_FILE.read_text(encoding="utf-8")
                except OSError as exc:
                    self._send(500, f"Dashboard unavailable: {exc}".encode(), "text/plain; charset=utf-8")
                    return
                document = (
                    document.replace("__REVIEW_TOKEN__", json.dumps(csrf_token))
                    .replace("__REVIEW_NONCE__", script_nonce)
                )
                self._send(200, document.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path == "/api/dashboard":
                if not self._authorized():
                    self._send(403, b'{"error":"Local dashboard token required"}', "application/json; charset=utf-8")
                    return
                try:
                    result = json.dumps(build_dashboard(queue_path, decisions_path), ensure_ascii=False).encode("utf-8")
                except (OSError, ValueError) as exc:
                    self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"), "application/json; charset=utf-8")
                    return
                self._send(200, result, "application/json; charset=utf-8")
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            if self.path != "/api/decisions":
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            if not self._authorized(require_origin=True):
                self._send(403, b'{"error":"Local origin and review token required"}', "application/json; charset=utf-8")
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, b'{"error":"Content-Type must be application/json"}', "application/json; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._send(413, b'{"error":"Request body is missing or too large"}', "application/json; charset=utf-8")
                return
            try:
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object")
                with decision_lock:
                    created = record_decision(
                        payload.get("article_id", ""),
                        payload.get("decision", ""),
                        reviewer_name,
                        payload.get("notes", ""),
                        secrets.token_urlsafe(18),
                        queue_path=queue_path,
                        decisions_path=decisions_path,
                    )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send(400, json.dumps({"error": str(exc)}).encode("utf-8"), "application/json; charset=utf-8")
                return
            result = json.dumps({"created": created, "reviewer": reviewer_name}).encode("utf-8")
            self._send(200, result, "application/json; charset=utf-8")

        def _authorized_host(self) -> bool:
            try:
                parsed = urlsplit(f"http://{self.headers.get('Host', '')}")
            except ValueError:
                return False
            return (
                parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.server.server_address[1]
            )

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"[review-dashboard] {self.address_string()} {fmt % args}", file=sys.stderr)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    args = parser.parse_args()
    try:
        server = create_server()
    except (OSError, RuntimeError) as exc:
        print(f"Could not start local review dashboard: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Local editorial dashboard listening at {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
