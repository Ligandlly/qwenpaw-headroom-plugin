# -*- coding: utf-8 -*-
"""Shared fixtures: a fake Headroom proxy and a fake provider manager."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fixtures import (  # noqa: E402  pylint: disable=wrong-import-position
    HEALTH,
    HISTORY,
    LIFETIME,
    STATS,
)


class FakeHeadroom:
    """A tiny HTTP server that answers the four Headroom endpoints."""

    def __init__(self) -> None:
        self.payloads: dict[str, Any] = {
            "/health": HEALTH,
            "/stats": STATS,
            "/stats-lifetime": LIFETIME,
            "/stats-history": HISTORY,
        }
        self.statuses: dict[str, int] = {}
        self.delay = 0.0
        self.calls: list[str] = []
        self.server: Optional[ThreadingHTTPServer] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "FakeHeadroom":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                path = self.path.split("?", 1)[0]
                outer.calls.append(self.path)
                if outer.delay:
                    time.sleep(outer.delay)
                status = outer.statuses.get(path, 200)
                body_payload = outer.payloads.get(path)
                if body_payload is None:
                    status, body_payload = 404, {"error": "not found"}
                if isinstance(body_payload, str):
                    body = body_payload.encode("utf-8")
                else:
                    body = json.dumps(body_payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except BrokenPipeError:  # pragma: no cover - client aborted
                    pass

            def log_message(self, *args: Any) -> None:  # noqa: D401, ANN002
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @property
    def base_url(self) -> str:
        assert self.server is not None
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def headroom() -> Any:
    fake = FakeHeadroom().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def fail_factory() -> Callable[[int], int]:
    """Return a factory producing an unused TCP port (for 'unreachable' tests)."""

    def factory(attempts: int = 1) -> int:
        import socket

        for _ in range(attempts):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            return port
        raise RuntimeError("no port")

    return factory


# ---------------------------------------------------------------------------
# Fake host provider manager
# ---------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, provider_id: str, base_url: str = "", api_key: str = "", is_custom: bool = True):
        self.id = provider_id
        self.name = provider_id
        self.base_url = base_url
        self.api_key = api_key
        self.is_custom = is_custom
        self.chat_model = "OpenAIChatModel"
        self.models: list[Any] = []


class FakeSlot:
    def __init__(self, provider_id: str, model: str) -> None:
        self.provider_id = provider_id
        self.model = model


class FakeManager:
    """Just enough of ProviderManager for routing tests."""

    def __init__(self, providers: Optional[dict[str, FakeProvider]] = None, active=None) -> None:
        self.providers = dict(providers or {})
        self.active = active
        self.added: list[Any] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.removed: list[str] = []
        self.default_calls: list[tuple[str, Any]] = []
        self.fail_update = False

    # -- reads -----------------------------------------------------------
    def get_active_model(self) -> Any:
        return self.active

    def get_provider(self, provider_id: str) -> Any:
        return self.providers.get(provider_id)

    # -- writes ----------------------------------------------------------
    async def add_custom_provider(self, info: Any) -> None:
        self.added.append(info)
        provider = FakeProvider(
            getattr(info, "id", "headroom"),
            base_url=getattr(info, "base_url", ""),
            api_key=getattr(info, "api_key", ""),
        )
        provider.models = list(getattr(info, "models", []) or [])
        self.providers[provider.id] = provider
        return provider

    async def update_provider_async(self, provider_id: str, config: dict[str, Any]) -> bool:
        self.updated.append((provider_id, config))
        if self.fail_update:
            return False
        provider = self.providers.get(provider_id)
        if provider is None:
            return False
        if "base_url" in config:
            provider.base_url = config["base_url"]
        if "api_key" in config:
            provider.api_key = config["api_key"]
        return True

    async def set_default_model(self, model_type: str, slot: Any) -> None:
        self.default_calls.append((model_type, slot))
        self.active = slot

    async def activate_model(self, provider_id: str, model: str, **kwargs: Any) -> None:
        self.active = FakeSlot(provider_id, model)

    async def remove_custom_provider_async(self, provider_id: str) -> bool:
        self.removed.append(provider_id)
        self.providers.pop(provider_id, None)
        return True
