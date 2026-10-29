"""HTTP server, authentication and traversal-safe static delivery."""

from __future__ import annotations

import mimetypes
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .api import ReadOnlyApi, json_response
from .config import ViewerConfig

STATIC_ROOT = (Path(__file__).resolve().parents[1] / "static").resolve()


def _static(path: str) -> tuple[int, str, bytes]:
    relative = "index.html" if path == "/" else unquote(path.removeprefix("/static/"))
    candidate = (STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(STATIC_ROOT)
    except ValueError:
        return json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
    if not candidate.is_file():
        return json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
        content_type += "; charset=utf-8"
    with candidate.open("rb") as handle:
        return HTTPStatus.OK, content_type, handle.read()


def _authorized(token: str, authorization: str, query: dict[str, list[str]]) -> bool:
    if not token:
        return True
    bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
    supplied = bearer or (query.get("token") or [""])[0]
    return secrets.compare_digest(supplied, token)


def make_handler(config: ViewerConfig) -> type[BaseHTTPRequestHandler]:
    api = ReadOnlyApi(config.profile, config.profile_home)

    class Handler(BaseHTTPRequestHandler):
        server_version = "HermesReadOnlyViewer/1.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path.startswith("/static/"):
                self._send(*_static(parsed.path))
            elif not _authorized(config.token, self.headers.get("Authorization", ""), query):
                self._send(*json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED))
            elif parsed.path == "/":
                self._send(*_static(parsed.path))
            else:
                self._send(*api.route(parsed.path, query))

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, message: str, *args: Any) -> None:
            clean = urlsplit(self.path).path
            sys.stderr.write(f"[viewer] {self.client_address[0]} {self.command} {clean}\n")

    return Handler


def serve(config: ViewerConfig) -> None:
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    server.daemon_threads = True
    print(f"Hermes read-only viewer listening on http://{config.host}:{config.port}", flush=True)
    if not config.token:
        print("WARNING: no token configured; expose this port only to a trusted LAN", file=sys.stderr)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
