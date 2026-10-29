from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools.viewer.viewer_app.config import ViewerConfig
from tools.viewer.viewer_app.server import ThreadingHTTPServer, make_handler


def test_http_auth_static_headers_and_traversal(profile: Path, monkeypatch) -> None:
    from tools.viewer.viewer_app.sources import status

    monkeypatch.setattr(status, "unit_state", lambda unit: "inactive")
    config = ViewerConfig("xiaozhi", profile, "127.0.0.1", 0, "viewer-passphrase")
    server = ThreadingHTTPServer((config.host, config.port), make_handler(config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/static/js/app.js", timeout=3) as response:
            assert response.status == 200
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/status", timeout=3)
        assert error.value.code == 401
        with pytest.raises(urllib.error.HTTPError) as traversal:
            urllib.request.urlopen(base + "/static/%2e%2e/viewer.py", timeout=3)
        assert traversal.value.code == 404
        request = urllib.request.Request(base + "/api/status")
        request.add_header("Authorization", "Bearer viewer-passphrase")
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
        assert payload["seed_total"] == 2
        with urllib.request.urlopen(base + "/?token=viewer-passphrase", timeout=3) as response:
            assert b"XIAOZHI" in response.read()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
