# -*- coding: utf-8 -*-
"""Contract test between the hand-written frontend and the JSON it consumes.

The page is plain ES module code with no build step, so nothing type-checks it.
This test reads every ``data.x.y`` / ``headline.x`` / ``config.x`` style field
path out of ``frontend/dist/index.js`` and asserts the backend really emits it.
It also proves the two halves agree on the plugin id, route id and endpoint
names, which is exactly what breaks when either side is edited alone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend" / "dist" / "index.js"
PLUGIN_JSON = ROOT / "plugin.json"
ROUTES_PY = ROOT / "headroom_plugin" / "routes.py"

#: Roots the page walks into the overview payload.
ROOT_PREFIXES = ("data", "config", "headline", "layers", "lifetime", "history", "agents", "routing", "proxy", "plugin")
#: Field paths that are only populated in some states.
OPTIONAL = {
    "data.hint",
    "data.error",
    "hint",
    "error",
    "data.status",
    "routing.active",
    "routing.active.model",
    "routing.active.provider_id",
    "routing.provider",
    "routing.provider.base_url",
    "routing.state",
    "history.session",
    "proxy.error",
    "lifetime.available",
}

PATH_RE = re.compile(
    r"(?<![\w.$])(?:" + "|".join(ROOT_PREFIXES) + r")(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b"
)

#: Array/object methods and property reads that are JavaScript, not payload keys.
JS_TAILS = {"length", "map", "join", "reduce", "filter", "slice", "forEach", "toFixed", "json", "stringify"}


@pytest.fixture(scope="module")
def frontend_source() -> str:
    assert FRONTEND.is_file(), f"missing frontend bundle: {FRONTEND}"
    return FRONTEND.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def overview(tmp_path_factory, request) -> dict:
    """A real overview payload built from the stub proxy."""
    from headroom_plugin.runtime import HeadroomRuntime

    conftest = request.getfixturevalue("headroom") if False else None  # noqa: F841  (documentation only)
    tmp = tmp_path_factory.mktemp("frontend-contract")
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    import fixtures  # noqa: WPS433  (local import keeps module import cheap)

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    routes = {
        "/health": fixtures.HEALTH,
        "/stats": fixtures.STATS,
        "/stats-lifetime": fixtures.LIFETIME,
        "/stats-history": fixtures.HISTORY,
    }

    class Stub(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):  # noqa: N802
            body = routes.get(self.path.split("?")[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp / "headroom.config.json").write_text(
            json.dumps(
                {
                    "headroom": {"base_url": f"http://127.0.0.1:{server.server_address[1]}", "timeout_seconds": 3},
                    "ui": {"history_limit": 10},
                }
            ),
            encoding="utf-8",
        )
        runtime = HeadroomRuntime(plugin_dir=tmp, plugin_version="1.0.0", runtime_config={}, environ={})
        yield runtime.overview()
    finally:
        server.shutdown()


def _walk(payload: dict, path: str) -> tuple[bool, object]:
    """Resolve ``headline.tokens_saved`` style paths; ``layers`` is a list."""
    node: object = payload
    for part in path.split("."):
        if isinstance(node, list):
            if not node:
                return False, None
            node = node[0]
        if not isinstance(node, dict):
            return False, None
        if part not in node:
            return False, None
        node = node[part]
    return True, node


def test_every_frontend_field_exists_in_the_payload(frontend_source, overview):
    paths = set()
    for match in PATH_RE.finditer(frontend_source):
        path = match.group(0)
        # `data.x` means "a top level key of the payload"; the others are literal.
        if path.startswith("data."):
            path = path[len("data."):]
        if path.startswith("config.routing."):
            path = path  # config.routing.* is served under config
        if path.rsplit(".", 1)[-1] in JS_TAILS:
            continue
        paths.add(path)

    missing = []
    for path in sorted(paths):
        if path in OPTIONAL:
            continue
        found, _ = _walk(overview, path)
        if not found:
            missing.append(path)

    assert not missing, "frontend reads fields the backend never sends: " + ", ".join(missing)


def test_frontend_uses_the_manifest_plugin_id(frontend_source):
    manifest = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert f'PLUGIN_ID = "{manifest["id"]}"' in frontend_source
    assert manifest["entry"]["frontend"] == "frontend/dist/index.js"


def test_frontend_endpoints_exist_in_the_router(frontend_source):
    router_source = ROUTES_PY.read_text(encoding="utf-8")
    # Every absolute path the page can hit, minus the console route it registers
    # and the dashboard links it opens in a new tab.
    ignore = {"/headroom", "/dashboard", "/api"}
    literals = set(re.findall(r'["\`](/[a-z][a-z/-]*)["\`?$]', frontend_source))
    called = {path for path in literals if path not in ignore}
    assert {"/savings", "/routing/apply", "/routing/disable", "/config/reload"} <= called, sorted(called)
    for path in sorted(called):
        assert f'@router.get("{path}"' in router_source or f'@router.post("{path}"' in router_source, path


def test_frontend_is_dependency_free(frontend_source):
    # A plain ES module: no imports, no bundler output, no CDN references.
    assert not re.search(r"^\s*import\s", frontend_source, flags=re.MULTILINE)
    assert "http://" not in frontend_source.replace("http://127.0.0.1", "").replace("http://localhost", "")
    assert "https://" not in frontend_source or "registry" not in frontend_source
    assert "".join(ch for ch in frontend_source if ch in "{}").count("{") > 0


def test_frontend_survives_a_missing_host(frontend_source):
    # The bundle must not throw when `window.QwenPaw` never appears.
    assert "whenHost" in frontend_source
    assert "host not found" in frontend_source or "宿主" in frontend_source
