# -*- coding: utf-8 -*-
"""Minimal, dependency-free client for the Headroom proxy HTTP API.

Only the read-only endpoints the dashboard needs are implemented.  Everything
runs on the standard library so an offline bundle needs no extra wheels.

Endpoint reference (headroom 0.42.x):

* ``GET /health``          — liveness, version, uptime, proxy mode.
* ``GET /stats``           — session counters, savings, layers, agent usage.
* ``GET /stats-lifetime``  — lifetime counters.
* ``GET /stats-history``   — per-request history (dashboard-gated on some
  builds; a 401/403 there is not fatal for the rest of the page).
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Mapping, Optional

LOGGER = logging.getLogger(__name__)

HEALTH_PATH = "/health"
STATS_PATH = "/stats"
LIFETIME_PATH = "/stats-lifetime"
HISTORY_PATH = "/stats-history"

#: Endpoints that some headroom builds protect behind a dashboard gate; losing
#: them only costs a section of the page, so they are fetched leniently.
OPTIONAL_PATHS = frozenset({HISTORY_PATH})


class HeadroomError(RuntimeError):
    """A Headroom request failed.

    ``kind`` is one of ``unreachable``, ``timeout``, ``http``, ``bad-json`` or
    ``bad-payload``; the UI uses it to pick a hint.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unreachable",
        status: Optional[int] = None,
        url: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.url = url

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "kind": self.kind,
            "status": self.status,
            "url": self.url,
        }


def _opener(use_env_proxy: bool) -> urllib.request.OpenerDirector:
    """Build an opener that ignores ``HTTP(S)_PROXY`` unless asked to use it.

    The proxy normally listens on loopback or a private container address; an
    inherited corporate proxy would turn a healthy connection into a timeout.
    """
    if use_env_proxy:
        return urllib.request.build_opener()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HeadroomClient:
    """Read-only access to one Headroom proxy."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        use_env_proxy: bool = False,
        opener: Optional[urllib.request.OpenerDirector] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout)
        self.use_env_proxy = bool(use_env_proxy)
        self._opener = opener or _opener(self.use_env_proxy)

    # -- plumbing --------------------------------------------------------
    def url_for(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        url = self.base_url + path
        if params:
            query = {
                key: value
                for key, value in params.items()
                if value is not None
            }
            if query:
                url += "?" + urllib.parse.urlencode(query)
        return url

    def get_json(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        url = self.url_for(path, params)
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "qwenpaw-headroom"},
            method="GET",
        )
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise HeadroomError(
                f"{path} returned HTTP {exc.code}",
                kind="http",
                status=exc.code,
                url=url,
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise HeadroomError(
                    f"{path} timed out after {self.timeout:g}s",
                    kind="timeout",
                    url=url,
                ) from exc
            raise HeadroomError(
                f"cannot reach {self.base_url}: {reason}",
                kind="unreachable",
                url=url,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise HeadroomError(
                f"{path} timed out after {self.timeout:g}s",
                kind="timeout",
                url=url,
            ) from exc
        except OSError as exc:
            raise HeadroomError(
                f"cannot reach {self.base_url}: {exc}",
                kind="unreachable",
                url=url,
            ) from exc

        elapsed_ms = (time.monotonic() - started) * 1000.0
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HeadroomError(
                f"{path} did not return JSON",
                kind="bad-json",
                url=url,
            ) from exc
        if not isinstance(payload, dict):
            raise HeadroomError(
                f"{path} returned {type(payload).__name__}, expected an object",
                kind="bad-payload",
                url=url,
            )
        LOGGER.debug("[headroom] GET %s ok in %.0fms", url, elapsed_ms)
        return payload

    # -- endpoints -------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self.get_json(HEALTH_PATH)

    def stats(self) -> dict[str, Any]:
        return self.get_json(STATS_PATH)

    def stats_lifetime(self) -> dict[str, Any]:
        return self.get_json(LIFETIME_PATH)

    def stats_history(self, limit: int = 60) -> dict[str, Any]:
        return self.get_json(HISTORY_PATH, {"limit": int(limit)})

    def probe(self) -> dict[str, Any]:
        """One cheap round-trip, reporting latency and a few health fields.

        Never raises: a dead proxy is a normal answer for the "test connection"
        button, so failures come back as ``{"ok": False, "error": {...}}``.
        """
        started = time.monotonic()
        try:
            payload = self.health()
        except HeadroomError as exc:
            return {
                "ok": False,
                "base_url": self.base_url,
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                "error": exc.as_dict(),
                "checked_at": _utc_now(),
            }
        elapsed_ms = (time.monotonic() - started) * 1000.0
        config = payload.get("config")
        return {
            "ok": True,
            "base_url": self.base_url,
            "latency_ms": round(elapsed_ms, 1),
            "version": payload.get("version"),
            "status": payload.get("status"),
            "ready": payload.get("ready"),
            "mode": config.get("mode") if isinstance(config, dict) else None,
            "uptime_seconds": payload.get("uptime_seconds"),
            "checked_at": _utc_now(),
        }

    def snapshot(self, *, history_limit: int = 60) -> dict[str, Any]:
        """Fetch every endpoint the dashboard needs, tolerating partial loss.

        ``/health`` decides connectivity; the rest degrade to ``None`` with an
        entry in ``errors`` so the page can still show what is available.
        """
        plan: list[tuple[str, Any]] = [
            ("health", lambda: self.health()),
            ("stats", lambda: self.stats()),
            ("lifetime", lambda: self.stats_lifetime()),
        ]
        if history_limit > 0:
            plan.append(("history", lambda: self.stats_history(history_limit)))

        results: dict[str, Any] = {}
        errors: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=len(plan)) as pool:
            futures = {
                name: pool.submit(call)
                for name, call in plan
            }
            for name, future in futures.items():
                try:
                    results[name] = future.result()
                except HeadroomError as exc:
                    errors[name] = exc.as_dict()
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    errors[name] = {
                        "message": str(exc),
                        "kind": "unexpected",
                        "status": None,
                        "url": "",
                    }
        health = results.get("health")
        if not isinstance(health, dict):
            fatal = errors.get("health") or {
                "message": "headroom /health is unavailable",
                "kind": "unreachable",
                "status": None,
                "url": self.url_for(HEALTH_PATH),
            }
            return {
                "connected": False,
                "base_url": self.base_url,
                "error": fatal,
                "errors": errors,
                "fetched_at": _utc_now(),
            }
        return {
            "connected": True,
            "base_url": self.base_url,
            "health": health,
            "stats": results.get("stats"),
            "lifetime": results.get("lifetime"),
            "history": results.get("history"),
            "errors": errors,
            "fetched_at": _utc_now(),
        }


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def client_from_settings(settings: Any) -> HeadroomClient:
    return HeadroomClient(
        settings.base_url,
        timeout=settings.timeout_seconds,
        use_env_proxy=settings.use_env_proxy,
    )
