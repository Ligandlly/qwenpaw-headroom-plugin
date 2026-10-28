# -*- coding: utf-8 -*-
"""HTTP surface of the plugin, mounted at ``/api/headroom``.

The browser only ever talks to these endpoints; it never reaches the Headroom
proxy directly.  That keeps CORS out of the picture, keeps the proxy URL in one
place (the shipped config file) and lets the page render a friendly hint when
the proxy is down.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .client import HeadroomError
from .routing import RoutingError
from .runtime import HeadroomRuntime

LOGGER = logging.getLogger(__name__)


def build_router(runtime: HeadroomRuntime) -> Any:
    """Create the FastAPI router bound to ``runtime``."""
    from fastapi import APIRouter, Query
    from fastapi.responses import JSONResponse

    router = APIRouter()

    def _error(status: int, exc: Any) -> JSONResponse:
        if isinstance(exc, HeadroomError):
            payload = exc.as_dict()
        elif isinstance(exc, RoutingError):
            payload = exc.as_dict()
        else:  # pragma: no cover - defensive
            payload = {"message": str(exc), "kind": "unexpected"}
        payload["ok"] = False
        return JSONResponse(status_code=status, content=payload)

    @router.get("/health", summary="Plugin and proxy health")
    async def health() -> dict[str, Any]:
        settings = await asyncio.to_thread(runtime.public_settings)
        probe: dict[str, Any]
        try:
            probe = await asyncio.to_thread(runtime.client().probe)
        except HeadroomError as exc:
            probe = {"ok": False, **exc.as_dict()}
        status = await asyncio.to_thread(runtime.status)
        return {
            "ok": bool(probe.get("ok")),
            "plugin": {
                "id": settings["plugin_id"],
                "version": settings.get("version", ""),
            },
            "config": settings,
            "proxy": probe,
            "routing": status,
        }

    @router.get("/config", summary="Effective plugin configuration")
    async def get_config() -> dict[str, Any]:
        return {
            "ok": True,
            "config": await asyncio.to_thread(runtime.public_settings),
            "routing": await asyncio.to_thread(runtime.status),
        }

    @router.post("/config/reload", summary="Re-read headroom.config.json")
    async def reload_config() -> dict[str, Any]:
        settings = await asyncio.to_thread(runtime.settings, refresh=True)
        return {
            "ok": True,
            "reloaded": True,
            "config": await asyncio.to_thread(runtime.public_settings),
            "warnings": list(settings.warnings),
        }

    @router.get("/savings", summary="Headroom token-saving overview")
    async def savings(
        history_limit: Optional[int] = Query(
            default=None,
            ge=0,
            le=500,
            description="How many recent requests to include in the chart",
        ),
        force: bool = Query(default=False, description="Bypass the 1.5s snapshot cache"),
    ) -> Any:
        try:
            return await asyncio.to_thread(
                runtime.overview,
                history_limit=history_limit,
                force=force,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.warning("[headroom] savings overview failed: %s", exc)
            return _error(503, exc)

    @router.get("/stats", summary="Raw Headroom stats payloads")
    async def raw_stats(
        history_limit: Optional[int] = Query(default=None, ge=0, le=500),
    ) -> Any:
        try:
            return {
                "ok": True,
                "snapshot": await asyncio.to_thread(
                    runtime.snapshot,
                    history_limit=history_limit,
                    force=True,
                ),
            }
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _error(503, exc)

    @router.post("/probe", summary="Probe the Headroom proxy once")
    async def probe() -> Any:
        try:
            return {
                "ok": True,
                "probe": await asyncio.to_thread(runtime.client().probe),
            }
        except HeadroomError as exc:
            return _error(503, exc)

    @router.get("/routing", summary="How model traffic is routed")
    async def get_routing() -> dict[str, Any]:
        status = await asyncio.to_thread(runtime.status)
        return {
            "ok": True,
            "routing": status.get("routing") or {},
            "proxy": {
                "ok": status.get("ok"),
                "connected": status.get("connected"),
                "base_url": status.get("base_url"),
                "version": status.get("version"),
            },
            "last_action": status.get("last_action") or {},
        }

    @router.post("/routing/apply", summary="Route model traffic through Headroom")
    async def apply_routing_endpoint() -> Any:
        try:
            report = await runtime.apply()
        except RoutingError as exc:
            return _error(409, exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _error(500, exc)
        return {"ok": True, **report}

    @router.post("/routing/disable", summary="Stop routing through Headroom")
    async def disable_routing_endpoint() -> Any:
        try:
            report = await runtime.disable()
        except RoutingError as exc:
            return _error(409, exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _error(500, exc)
        return {"ok": True, **report}

    @router.get("/settings/preview", summary="What routing would configure")
    async def routing_preview() -> Any:
        from .routing import resolve_target

        try:
            settings = await asyncio.to_thread(runtime.settings)
            target = await asyncio.to_thread(_resolve, settings)
        except RoutingError as exc:
            return _error(409, exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            return _error(500, exc)
        return {"ok": True, "target": target}

    def _resolve(settings: Any) -> dict[str, Any]:
        from .routing import provider_manager, resolve_target

        return resolve_target(provider_manager(), settings).as_dict()

    return router
