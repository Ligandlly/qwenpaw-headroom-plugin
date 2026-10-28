# -*- coding: utf-8 -*-
"""QwenPaw Headroom plugin — backend entry point.

Registers:

* ``/api/headroom/*`` — read-only views of the Headroom proxy (savings,
  health, routing status) plus two actions that switch model traffic between
  QwenPaw's own providers and the proxy.
* a startup hook that re-applies routing when ``routing.enabled`` is true, so
  an offline install keeps compressing after a restart.

The Headroom URL lives in ``headroom.config.json`` next to this file.  Edit it
before packaging a distribution: it is the one place an air-gapped install
needs to point at its proxy.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_plugin_dir = Path(__file__).resolve().parent
_plugin_dir_str = str(_plugin_dir)
if _plugin_dir_str not in sys.path:
    sys.path.insert(0, _plugin_dir_str)

# QwenPaw execs this file as a plain module.  On plugin reload the previous
# ``headroom_plugin`` package can still be cached in ``sys.modules``; evict only
# the modules that belong to this plugin directory so the new source is used.
for _module_name in list(sys.modules):
    if _module_name != "headroom_plugin" and not _module_name.startswith(
        "headroom_plugin.",
    ):
        continue
    _module = sys.modules.get(_module_name)
    _module_file = getattr(_module, "__file__", None) or ""
    if _module_file and os.path.realpath(_module_file).startswith(
        os.path.realpath(_plugin_dir_str) + os.sep,
    ):
        del sys.modules[_module_name]

from qwenpaw.plugins.api import PluginApi  # noqa: E402

from headroom_plugin.config import PLUGIN_ID, runtime_config_from_api  # noqa: E402
from headroom_plugin.routes import build_router  # noqa: E402
from headroom_plugin.runtime import HeadroomRuntime  # noqa: E402

logger = logging.getLogger("qwenpaw.headroom")

ROUTE_PREFIX = "/headroom"
STARTUP_HOOK = "headroom_route"


def plugin_version() -> str:
    try:
        manifest = json.loads(
            (_plugin_dir / "plugin.json").read_text(encoding="utf-8"),
        )
    except (OSError, ValueError):
        return ""
    version = manifest.get("version")
    return str(version or "")


class HeadroomPlugin:
    """Expose the Headroom savings page and route model traffic through it."""

    def register(self, api: PluginApi) -> None:
        """Mount the router and (optionally) route traffic at startup."""
        runtime_config = runtime_config_from_api(api)
        runtime = HeadroomRuntime(
            plugin_dir=_plugin_dir,
            plugin_version=plugin_version(),
            runtime_config=runtime_config,
        )
        self.runtime = runtime

        router = build_router(runtime)
        api.register_http_router(
            router,
            prefix=ROUTE_PREFIX,
            tags=["headroom"],
        )

        async def startup() -> None:
            await runtime.apply_on_startup()

        register_hook = getattr(api, "register_startup_hook", None)
        if callable(register_hook):
            register_hook(STARTUP_HOOK, startup, priority=60)

        settings = runtime.settings()
        logger.info(
            "Headroom plugin registered: GET /api/headroom/savings "
            "(proxy %s, routing %s, config %s)",
            settings.base_url,
            "enabled" if settings.routing_enabled else "disabled",
            settings.config_path,
        )
        for warning in settings.warnings:
            logger.warning("Headroom config: %s", warning)


plugin = HeadroomPlugin()
