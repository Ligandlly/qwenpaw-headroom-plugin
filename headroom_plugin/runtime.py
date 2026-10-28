# -*- coding: utf-8 -*-
"""Plugin-wide runtime state: settings, client, routing, cached snapshots.

One instance is created in ``plugin.py`` and shared by the HTTP router and the
startup hook.  Configuration is re-read when the shipped file changes on disk,
so an operator who edits ``headroom.config.json`` and reloads the plugin page
sees the new URL without restarting QwenPaw.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from . import config as config_module
from .client import HeadroomClient, client_from_settings
from .routing import RoutingError, apply_routing, disable_routing, routing_status
from .savings import build_overview

LOGGER = logging.getLogger(__name__)

#: A cached snapshot is reused for this long, so a page with several widgets
#: does not fan out to the proxy on every render.
SNAPSHOT_TTL_SECONDS = 1.5


class HeadroomRuntime:
    def __init__(
        self,
        *,
        plugin_dir: Optional[Path] = None,
        plugin_version: str = "",
        runtime_config: Optional[Mapping[str, Any]] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.plugin_dir = Path(plugin_dir or Path(__file__).resolve().parent.parent)
        self.plugin_version = plugin_version
        self._runtime_config = dict(runtime_config or {})
        self._environ = environ
        self._settings: Optional[config_module.Settings] = None
        self._settings_mtime: Optional[float] = None
        self._client: Optional[HeadroomClient] = None
        self._client_key: tuple[Any, ...] = ()
        self._snapshot: Optional[dict[str, Any]] = None
        self._snapshot_at = 0.0
        self._snapshot_lock = threading.Lock()
        self._routing_lock = asyncio.Lock()
        self._last_action: dict[str, Any] = {}

    # -- settings --------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.plugin_dir / config_module.CONFIG_FILENAME

    def _config_mtime(self) -> Optional[float]:
        try:
            return self.config_path.stat().st_mtime
        except OSError:
            return None

    def settings(self, *, refresh: bool = False) -> config_module.Settings:
        mtime = self._config_mtime()
        if refresh or self._settings is None or mtime != self._settings_mtime:
            self._settings = config_module.load_settings(
                plugin_dir=self.plugin_dir,
                runtime_config=self._runtime_config,
                environ=self._environ,
                config_path=self.config_path,
            )
            self._settings_mtime = mtime
            self._client = None
            settings = self._settings
            for warning in settings.warnings:
                LOGGER.warning("[headroom] config: %s", warning)
            LOGGER.info(
                "[headroom] configuration loaded from %s (base_url=%s, source=%s)",
                settings.config_path,
                settings.base_url,
                settings.source("headroom.base_url"),
            )
        assert self._settings is not None
        return self._settings

    def update_runtime_config(self, runtime_config: Mapping[str, Any]) -> None:
        self._runtime_config = dict(runtime_config or {})
        self.settings(refresh=True)

    def client(self) -> HeadroomClient:
        settings = self.settings()
        key = (
            settings.base_url,
            settings.timeout_seconds,
            settings.use_env_proxy,
        )
        if self._client is None or key != self._client_key:
            self._client = client_from_settings(settings)
            self._client_key = key
        return self._client

    def public_settings(self) -> dict[str, Any]:
        settings = self.settings()
        payload = settings.public()
        payload["version"] = self.plugin_version
        return payload

    # -- data ------------------------------------------------------------
    def snapshot(self, *, history_limit: Optional[int] = None, force: bool = False) -> dict[str, Any]:
        settings = self.settings()
        limit = settings.history_limit if history_limit is None else history_limit
        now = time.monotonic()
        with self._snapshot_lock:
            fresh = (
                self._snapshot is not None
                and not force
                and (now - self._snapshot_at) < SNAPSHOT_TTL_SECONDS
            )
            if fresh:
                return self._snapshot  # type: ignore[return-value]
            snapshot = self.client().snapshot(history_limit=limit)
            self._snapshot = snapshot
            self._snapshot_at = now
            return snapshot

    def overview(self, *, history_limit: Optional[int] = None, force: bool = False) -> dict[str, Any]:
        settings = self.settings()
        snapshot = self.snapshot(history_limit=history_limit, force=force)
        payload = build_overview(snapshot, settings)
        payload.setdefault("plugin", {})
        payload["plugin"] = {
            "id": settings.plugin_id,
            "version": self.plugin_version,
        }
        status = self.status()
        payload["routing"] = status.get("routing") or {}
        payload["proxy"] = {
            "ok": status.get("ok"),
            "connected": status.get("connected"),
            "base_url": status.get("base_url"),
            "version": status.get("version"),
            "latency_ms": status.get("latency_ms"),
            "checked_at": status.get("checked_at"),
            "config_sources": status.get("config_sources") or {},
        }
        if status.get("error"):
            payload["proxy"]["error"] = status["error"]
        if self._last_action:
            payload["last_action"] = self._last_action
        return payload

    def status(self) -> dict[str, Any]:
        """One call the page uses to render its header (connectivity + routing)."""
        settings = self.settings()
        probe = self.client().probe()
        try:
            routing = routing_status(settings)
        except RoutingError as exc:  # pragma: no cover - defensive
            routing = {
                "host_available": False,
                "error": exc.as_dict(),
                "routed": False,
                "provider": {"exists": False},
                "active": {},
                "state": {},
            }
        payload: dict[str, Any] = {
            "ok": bool(probe.get("ok")),
            "connected": bool(probe.get("ok")),
            "base_url": settings.base_url,
            "api_base": settings.api_base,
            "dashboard_url": settings.dashboard_url,
            "version": probe.get("version"),
            "proxy_status": probe.get("status"),
            "ready": probe.get("ready"),
            "mode": probe.get("mode"),
            "uptime_seconds": probe.get("uptime_seconds"),
            "latency_ms": probe.get("latency_ms"),
            "checked_at": probe.get("checked_at"),
            "config_sources": {
                "base_url": settings.source("headroom.base_url"),
                "config_path": str(settings.config_path),
                "config_found": settings.config_found,
            },
            "routing": routing,
            "last_action": dict(self._last_action),
        }
        if probe.get("error"):
            payload["error"] = probe["error"]
        return payload

    # -- routing ---------------------------------------------------------
    async def apply(self) -> dict[str, Any]:
        settings = self.settings(refresh=True)
        async with self._routing_lock:
            try:
                report = await apply_routing(settings)
            except RoutingError as exc:
                LOGGER.warning("[headroom] routing not applied: %s", exc)
                self._last_action = {
                    "action": "apply",
                    "ok": False,
                    "at": _now(),
                    "error": exc.as_dict(),
                }
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                LOGGER.exception("[headroom] routing failed")
                self._last_action = {
                    "action": "apply",
                    "ok": False,
                    "at": _now(),
                    "error": {"message": str(exc), "kind": "unexpected"},
                }
                raise
            self._last_action = {
                "action": "apply",
                "ok": True,
                "at": _now(),
                "messages": report.get("messages", []),
            }
            return report

    async def disable(self) -> dict[str, Any]:
        settings = self.settings(refresh=True)
        async with self._routing_lock:
            report = await disable_routing(settings)
            self._last_action = {
                "action": "disable",
                "ok": True,
                "at": _now(),
                "messages": report.get("messages", []),
            }
            return report

    async def apply_on_startup(self) -> None:
        """Route traffic at boot when the config asks for it."""
        settings = self.settings(refresh=True)
        if not settings.routing_enabled:
            LOGGER.info("[headroom] routing disabled in config; not applied")
            return
        try:
            report = await apply_routing(settings)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # A proxy that is not up yet must not fail the plugin load.
            LOGGER.warning("[headroom] startup routing skipped: %s", exc)
            return
        LOGGER.info(
            "[headroom] routed %s/%s through %s",
            report.get("target", {}).get("provider_id"),
            report.get("target", {}).get("model"),
            report.get("target", {}).get("base_url"),
        )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_runtime_config() -> dict[str, Any]:
    """Read ``plugins.headroom`` straight from the host config file."""
    try:  # pragma: no cover - host dependent
        from qwenpaw.config.utils import load_config

        root = load_config()
        plugins = getattr(root, "plugins", None)
        if isinstance(plugins, Mapping):
            block = plugins.get(config_module.PLUGIN_ID)
            if isinstance(block, Mapping):
                return {k: v for k, v in block.items() if k != "enabled"}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.debug("[headroom] cannot read plugins.%s: %s", config_module.PLUGIN_ID, exc)
    return {}


def environ_snapshot() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.startswith("HEADROOM_")}
