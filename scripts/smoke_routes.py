# -*- coding: utf-8 -*-
"""End-to-end smoke check for the HTTP surface.

Run it with a Python that has FastAPI and HTTPX — i.e. QwenPaw's own venv::

    /home/lly/.local/share/uv/tools/qwenpaw/bin/python scripts/smoke_routes.py

It boots a stub Headroom server, mounts the plugin router on a throwaway
FastAPI app and drives every endpoint.  This is the check to run on an offline
machine where ``pytest`` may not exist.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import fixtures  # noqa: E402  (tests/fixtures.py)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from headroom_plugin.routes import build_router  # noqa: E402
from headroom_plugin.runtime import HeadroomRuntime  # noqa: E402

ROUTES = {
    "/health": fixtures.HEALTH,
    "/stats": fixtures.STATS,
    "/stats-lifetime": fixtures.LIFETIME,
    "/stats-history": fixtures.HISTORY,
}


class Stub(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *_args):  # keep the output clean
        return

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        Stub.calls.append(self.path)
        body = ROUTES.get(path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"detail":"not found"}')
            return
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def main() -> int:
    server, base_url = serve()
    tmp = Path(tempfile.mkdtemp(prefix="headroom-smoke-"))
    (tmp / "headroom.config.json").write_text(
        json.dumps(
            {
                "headroom": {"base_url": base_url, "timeout_seconds": 3},
                "routing": {"enabled": False},
                "ui": {"history_limit": 10},
            }
        ),
        encoding="utf-8",
    )

    runtime = HeadroomRuntime(plugin_dir=tmp, plugin_version="1.0.0", runtime_config={}, environ={})
    app = FastAPI()
    app.include_router(build_router(runtime), prefix="/headroom")
    client = TestClient(app)

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        checks.append((name, bool(condition), detail))

    try:
        body = client.get("/headroom/health").json()
        check("GET /health ok", body.get("ok") is True, json.dumps(body.get("proxy"))[:120])
        check("GET /health plugin identity", body.get("plugin", {}).get("id") == "headroom")
        check(
            "GET /health reports proxy version",
            body.get("proxy", {}).get("version") == fixtures.HEALTH["version"],
            str(body.get("proxy", {}).get("version")),
        )

        body = client.get("/headroom/config").json()
        config = body.get("config") or {}
        check("GET /config base_url", config.get("base_url") == base_url, str(config.get("base_url")))
        check("GET /config api_base", config.get("api_base") == f"{base_url}/v1", str(config.get("api_base")))
        check("GET /config hides an inline secret", "sk-" not in json.dumps(body))

        check("POST /config/reload", client.post("/headroom/config/reload").json().get("ok") is True)

        body = client.get("/headroom/savings").json()
        headline = body.get("headline") or {}
        check("GET /savings tokens_saved", headline.get("tokens_saved") == 2163652, str(headline.get("tokens_saved")))
        check("GET /savings layers", len(body.get("layers") or []) == 4)
        check("GET /savings history points", len((body.get("history") or {}).get("points") or []) == 3)
        check("GET /savings agents", len((body.get("agents") or {}).get("rows") or []) == 2)
        check("GET /savings proxy block", (body.get("proxy") or {}).get("ok") is True)
        check("GET /savings routing block", "host_available" in (body.get("routing") or {}))

        before = len(Stub.calls)
        client.get("/headroom/savings?history_limit=2&force=true")
        check(
            "history_limit is forwarded",
            any("limit=2" in call for call in Stub.calls[before:]),
            str(Stub.calls[before:]),
        )

        body = client.get("/headroom/stats").json()
        check("GET /stats snapshot", body.get("snapshot", {}).get("health", {}).get("status") == "healthy")

        body = client.post("/headroom/probe").json()
        check("POST /probe", (body.get("probe") or {}).get("ok") is True, str((body.get("probe") or {}).get("latency_ms")))

        body = client.get("/headroom/routing").json()
        routing = body.get("routing") or {}
        check("GET /routing", body.get("ok") is True, json.dumps(body)[:160])
        check("GET /routing names the provider", routing.get("provider_id") == "headroom")
        check("GET /routing exposes host_available", "host_available" in routing, str(routing.get("host_available")))
        check("GET /routing has_drift", routing.get("has_drift") is False)
        check("GET /routing proxy block", (body.get("proxy") or {}).get("connected") is True)

        response = client.post("/headroom/routing/apply")
        check(
            "POST /routing/apply without a model is a clean error",
            response.status_code in (409, 500, 503) and response.json().get("ok") is False,
            f"{response.status_code} {response.json().get('kind')}",
        )

        response = client.get("/headroom/settings/preview")
        check(
            "GET /settings/preview degrades cleanly",
            response.status_code in (409, 500, 503),
            str(response.status_code),
        )

        # Now point the runtime at a dead port and re-check the degraded page.
        runtime.update_runtime_config({"base_url": "http://127.0.0.1:1", "timeout_seconds": 1})
        body = client.get("/headroom/savings?force=true").json()
        check("degraded page stays renderable", body.get("connected") is False)
        check("degraded page keeps section keys", "layers" in body and "headline" in body)
        check("degraded page has a hint", (body.get("hint") or {}).get("kind") in {"unreachable", "timeout"}, str(body.get("hint", {}).get("kind")))
    finally:
        server.shutdown()

    width = max(len(name) for name, _, _ in checks)
    failures = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        line = f"{mark}  {name.ljust(width)}"
        if detail and not ok:
            line += f"  <- {detail}"
        print(line)

    print(f"\n{len(checks) - failures}/{len(checks)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
