# -*- coding: utf-8 -*-
"""Layered configuration for the Headroom plugin.

Three layers, later wins:

1. ``DEFAULTS`` — a working loopback setup, so a fresh install just works.
2. ``headroom.config.json`` next to ``plugin.py`` — **this is the file to edit
   when building an offline / air-gapped distribution**: hardcode the Headroom
   proxy URL here and it travels with the zip.
3. QwenPaw runtime config (``~/.qwenpaw/config.json`` → ``plugins.headroom``) —
   lets a single machine override the shipped value from the Console.
4. Environment (``HEADROOM_BASE_URL`` …) — last resort for containers.

The JSON file may contain ``//`` and ``/* */`` comments (it is read with a
quote-aware stripper), because the intended audience is a human editing a
handed-out zip, not a script.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Optional

LOGGER = logging.getLogger(__name__)

PLUGIN_ID = "headroom"
CONFIG_FILENAME = "headroom.config.json"

ENV_BASE_URL = "HEADROOM_BASE_URL"
ENV_BASE_URL_ALIAS = "QWENPAW_HEADROOM_URL"
ENV_TIMEOUT = "HEADROOM_TIMEOUT_SECONDS"
ENV_ROUTING = "HEADROOM_ROUTING_ENABLED"
ENV_HISTORY_LIMIT = "HEADROOM_HISTORY_LIMIT"

#: ``dotted key -> Settings attribute``.  Every tunable the plugin understands
#: is listed here exactly once; unknown keys are ignored (and reported) so a
#: stale config never breaks startup.
KEY_MAP: dict[str, str] = {
    "headroom.base_url": "base_url",
    "headroom.timeout_seconds": "timeout_seconds",
    "headroom.use_env_proxy": "use_env_proxy",
    "ui.refresh_seconds": "refresh_seconds",
    "ui.history_limit": "history_limit",
    "ui.dashboard_path": "dashboard_path",
    "routing.enabled": "routing_enabled",
    "routing.provider_id": "provider_id",
    "routing.provider_name": "provider_name",
    "routing.api_path": "api_path",
    "routing.chat_model": "chat_model",
    "routing.model": "model",
    "routing.adopt_active_model": "adopt_active_model",
    "routing.api_key": "api_key",
    "routing.restore_on_disable": "restore_on_disable",
    "routing.state_file": "state_file",
}

#: Flat spellings accepted in the QwenPaw ``plugins.headroom`` block, so a user
#: can write ``{"base_url": "..."}`` instead of the nested form.
FLAT_ALIASES: dict[str, str] = {
    "base_url": "headroom.base_url",
    "url": "headroom.base_url",
    "headroom_url": "headroom.base_url",
    "proxy_url": "headroom.base_url",
    "timeout_seconds": "headroom.timeout_seconds",
    "timeout": "headroom.timeout_seconds",
    "use_env_proxy": "headroom.use_env_proxy",
    "enabled": "routing.enabled",
    "routing_enabled": "routing.enabled",
    "provider_id": "routing.provider_id",
    "provider_name": "routing.provider_name",
    "api_path": "routing.api_path",
    "chat_model": "routing.chat_model",
    "model": "routing.model",
    "adopt_active_model": "routing.adopt_active_model",
    "api_key": "routing.api_key",
    "restore_on_disable": "routing.restore_on_disable",
    "state_file": "routing.state_file",
    "refresh_seconds": "ui.refresh_seconds",
    "history_limit": "ui.history_limit",
    "dashboard_path": "ui.dashboard_path",
}

DEFAULTS: dict[str, Any] = {
    "headroom": {
        "base_url": "http://127.0.0.1:8787",
        "timeout_seconds": 5.0,
        "use_env_proxy": False,
    },
    "routing": {
        "enabled": True,
        "provider_id": "headroom",
        "provider_name": "Headroom 压缩代理",
        "api_path": "/v1",
        "chat_model": "OpenAIChatModel",
        "model": "",
        "adopt_active_model": True,
        "api_key": "",
        "restore_on_disable": True,
        "state_file": "",
    },
    "ui": {
        "refresh_seconds": 20,
        "history_limit": 60,
        "dashboard_path": "/dashboard",
    },
}

_PROVIDER_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz0123456789-_",
)


class ConfigError(ValueError):
    """Raised when a layer cannot be parsed at all."""


# ---------------------------------------------------------------------------
# JSON with comments
# ---------------------------------------------------------------------------


def strip_json_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments while respecting string literals.

    A hand-edited config that ships inside a distribution is much easier to
    trust with annotations, and ``http://`` inside a value survives because
    URLs never start a comment here (the stripper only treats ``//`` as a
    comment when it is outside a string).
    """
    out: list[str] = []
    i = 0
    length = len(text)
    in_string = False
    escaped = False
    while i < length:
        char = text[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and i + 1 < length:
            nxt = text[i + 1]
            if nxt == "/":
                while i < length and text[i] != "\n":
                    i += 1
                continue
            if nxt == "*":
                i += 2
                while i + 1 < length and not (
                    text[i] == "*" and text[i + 1] == "/"
                ):
                    i += 1
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


def parse_config_text(text: str) -> dict[str, Any]:
    """Parse a config file body into a mapping."""
    try:
        payload = json.loads(strip_json_comments(text))
    except ValueError as exc:  # json.JSONDecodeError is a ValueError
        raise ConfigError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError("config root must be a JSON object")
    return payload


def read_config_file(path: Path) -> tuple[dict[str, Any], Optional[str]]:
    """Return ``(payload, error)``; never raises."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"cannot read {path.name}: {exc}"
    try:
        return parse_config_text(text), None
    except ConfigError as exc:
        return {}, f"{path.name}: {exc}"


# ---------------------------------------------------------------------------
# Layer merging
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _normalize_layer(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a user-supplied block into the nested ``DEFAULTS`` shape.

    ``plugins.headroom`` is usually flat (``{"base_url": "..."}``) while the
    shipped file is nested.  Accept both: known section names are taken as-is,
    every other known key is routed to the section that owns it.
    """
    sections = set(DEFAULTS)
    nested: dict[str, Any] = {}
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if key in sections and isinstance(value, Mapping):
            nested[str(key)] = dict(value)
        else:
            flat[str(key)] = value
    for key, value in flat.items():
        dotted = FLAT_ALIASES.get(key)
        if dotted is None and key in KEY_MAP:
            dotted = key
        if dotted is None:
            continue
        section, leaf = dotted.split(".", 1)
        nested.setdefault(section, {})[leaf] = value
    return nested


def _collect_dotted(payload: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in payload.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping):
            found.update(_collect_dotted(value, dotted + "."))
        else:
            found[dotted] = value
    return found


def _env_layer(environ: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
    layer: dict[str, Any] = {}
    origins: dict[str, str] = {}

    def put(dotted: str, value: Any, origin: str) -> None:
        section, leaf = dotted.split(".", 1)
        layer.setdefault(section, {})[leaf] = value
        origins[dotted] = origin

    base_url = (environ.get(ENV_BASE_URL) or "").strip()
    if base_url:
        put("headroom.base_url", base_url, f"env:{ENV_BASE_URL}")
    else:
        alias = (environ.get(ENV_BASE_URL_ALIAS) or "").strip()
        if alias:
            put("headroom.base_url", alias, f"env:{ENV_BASE_URL_ALIAS}")

    raw_timeout = (environ.get(ENV_TIMEOUT) or "").strip()
    if raw_timeout:
        put("headroom.timeout_seconds", raw_timeout, f"env:{ENV_TIMEOUT}")

    raw_routing = (environ.get(ENV_ROUTING) or "").strip()
    if raw_routing:
        put("routing.enabled", raw_routing, f"env:{ENV_ROUTING}")

    raw_history = (environ.get(ENV_HISTORY_LIMIT) or "").strip()
    if raw_history:
        put("ui.history_limit", raw_history, f"env:{ENV_HISTORY_LIMIT}")
    return layer, origins


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def as_bool(value: Any, default: bool) -> tuple[bool, Optional[str]]:
    if isinstance(value, bool):
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value), None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True, None
        if text in {"0", "false", "no", "off", "disabled"}:
            return False, None
    return default, f"expected a boolean, got {value!r}"


def as_float(
    value: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> tuple[float, Optional[str]]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default, f"expected a number, got {value!r}"
    if number != number:  # NaN
        return default, "expected a finite number"
    clamped = min(max(number, minimum), maximum)
    if clamped != number:
        return clamped, f"clamped to [{minimum}, {maximum}]"
    return clamped, None


def as_int(
    value: Any,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> tuple[int, Optional[str]]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default, f"expected an integer, got {value!r}"
    clamped = min(max(number, minimum), maximum)
    if clamped != number:
        return clamped, f"clamped to [{minimum}, {maximum}]"
    return clamped, None


def as_text(value: Any, default: str) -> tuple[str, Optional[str]]:
    if value is None:
        return default, None
    if not isinstance(value, str):
        return default, f"expected a string, got {value!r}"
    return value.strip(), None


def normalize_url(url: str) -> str:
    """Return an http(s) URL without a trailing slash.

    A bare ``host:port`` (no scheme) is upgraded to ``http://host:port``, so an
    operator hand-editing ``headroom.config.json`` inside an offline bundle can
    write either form.
    """
    text = (url or "").strip()
    if not text:
        raise ConfigError("headroom URL is empty")
    if "://" not in text:
        text = "http://" + text
    scheme, separator, rest = text.partition("://")
    lowered = scheme.lower()
    if lowered not in ("http", "https"):
        raise ConfigError(f"headroom URL must use http:// or https:// (got {text!r})")
    return f"{lowered}{separator}{rest}".rstrip("/")
# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Effective, validated plugin configuration."""

    base_url: str = "http://127.0.0.1:8787"
    timeout_seconds: float = 5.0
    use_env_proxy: bool = False
    refresh_seconds: int = 20
    history_limit: int = 60
    dashboard_path: str = "/dashboard"
    routing_enabled: bool = True
    provider_id: str = "headroom"
    provider_name: str = "Headroom 压缩代理"
    api_path: str = "/v1"
    chat_model: str = "OpenAIChatModel"
    model: str = ""
    adopt_active_model: bool = True
    api_key: str = ""
    restore_on_disable: bool = True
    state_file: str = ""
    sources: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    config_path: str = ""
    config_found: bool = False
    config_error: str = ""
    plugin_id: str = PLUGIN_ID

    # -- derived ---------------------------------------------------------
    @property
    def api_base(self) -> str:
        """Base URL the *model provider* should use (``.../v1``)."""
        base = self.base_url.rstrip("/")
        path = (self.api_path or "").strip()
        if not path or path == "/":
            return base
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")
        if base.endswith(path):
            return base
        return base + path

    @property
    def dashboard_url(self) -> str:
        path = (self.dashboard_path or "").strip()
        if not path:
            return self.base_url
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url.rstrip("/") + path

    @property
    def api_key_set(self) -> bool:
        return bool(self.api_key)

    def masked_api_key(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}…{self.api_key[-4:]}"

    def source(self, dotted: str) -> str:
        return self.sources.get(dotted, "default")

    # -- serialization ---------------------------------------------------
    def public(self) -> dict[str, Any]:
        """Config snapshot safe to hand to the browser."""
        return {
            "plugin_id": self.plugin_id,
            "base_url": self.base_url,
            "api_base": self.api_base,
            "dashboard_url": self.dashboard_url,
            "timeout_seconds": self.timeout_seconds,
            "use_env_proxy": self.use_env_proxy,
            "ui": {
                "refresh_seconds": self.refresh_seconds,
                "history_limit": self.history_limit,
                "dashboard_path": self.dashboard_path,
            },
            "routing": {
                "enabled": self.routing_enabled,
                "provider_id": self.provider_id,
                "provider_name": self.provider_name,
                "api_path": self.api_path,
                "chat_model": self.chat_model,
                "model": self.model,
                "adopt_active_model": self.adopt_active_model,
                "restore_on_disable": self.restore_on_disable,
                "api_key_set": self.api_key_set,
                "api_key_masked": self.masked_api_key(),
            },
            "config_file": {
                "path": self.config_path,
                "found": self.config_found,
                "error": self.config_error,
            },
            "sources": dict(self.sources),
            "warnings": list(self.warnings),
        }


def _settings_from_merged(
    merged: Mapping[str, Any],
    sources: Mapping[str, str],
    *,
    config_path: Path,
    config_found: bool,
    config_error: str,
    extra_warnings: tuple[str, ...] = (),
) -> Settings:
    warnings: list[str] = list(extra_warnings)

    def section(name: str) -> Mapping[str, Any]:
        value = merged.get(name)
        return value if isinstance(value, Mapping) else {}

    headroom = section("headroom")
    routing = section("routing")
    ui = section("ui")

    base_url, issue = as_text(
        headroom.get("base_url"),
        DEFAULTS["headroom"]["base_url"],
    )
    if issue:
        warnings.append(f"headroom.base_url: {issue}")
    try:
        base_url = normalize_url(base_url)
    except ConfigError as exc:
        warnings.append(f"headroom.base_url: {exc}; using default")
        base_url = DEFAULTS["headroom"]["base_url"]

    timeout, issue = as_float(
        headroom.get("timeout_seconds"),
        DEFAULTS["headroom"]["timeout_seconds"],
        minimum=0.5,
        maximum=60.0,
    )
    if issue:
        warnings.append(f"headroom.timeout_seconds: {issue}")

    use_env_proxy, issue = as_bool(
        headroom.get("use_env_proxy"),
        DEFAULTS["headroom"]["use_env_proxy"],
    )
    if issue:
        warnings.append(f"headroom.use_env_proxy: {issue}")

    refresh, issue = as_int(
        ui.get("refresh_seconds"),
        DEFAULTS["ui"]["refresh_seconds"],
        minimum=3,
        maximum=600,
    )
    if issue:
        warnings.append(f"ui.refresh_seconds: {issue}")

    history_limit, issue = as_int(
        ui.get("history_limit"),
        DEFAULTS["ui"]["history_limit"],
        minimum=0,
        maximum=500,
    )
    if issue:
        warnings.append(f"ui.history_limit: {issue}")

    dashboard_path, issue = as_text(
        ui.get("dashboard_path"),
        DEFAULTS["ui"]["dashboard_path"],
    )
    if issue:
        warnings.append(f"ui.dashboard_path: {issue}")

    enabled, issue = as_bool(
        routing.get("enabled"),
        DEFAULTS["routing"]["enabled"],
    )
    if issue:
        warnings.append(f"routing.enabled: {issue}")

    provider_id, issue = as_text(
        routing.get("provider_id"),
        DEFAULTS["routing"]["provider_id"],
    )
    if issue:
        warnings.append(f"routing.provider_id: {issue}")
    provider_id = provider_id.lower().replace(" ", "-")
    if not provider_id or set(provider_id) - _PROVIDER_ID_CHARS:
        warnings.append(
            f"routing.provider_id: invalid id {provider_id!r}; using default",
        )
        provider_id = DEFAULTS["routing"]["provider_id"]

    provider_name, issue = as_text(
        routing.get("provider_name"),
        DEFAULTS["routing"]["provider_name"],
    )
    if issue:
        warnings.append(f"routing.provider_name: {issue}")

    api_path, issue = as_text(
        routing.get("api_path"),
        DEFAULTS["routing"]["api_path"],
    )
    if issue:
        warnings.append(f"routing.api_path: {issue}")

    chat_model, issue = as_text(
        routing.get("chat_model"),
        DEFAULTS["routing"]["chat_model"],
    )
    if issue:
        warnings.append(f"routing.chat_model: {issue}")

    model, issue = as_text(
        routing.get("model"),
        DEFAULTS["routing"]["model"],
    )
    if issue:
        warnings.append(f"routing.model: {issue}")

    adopt, issue = as_bool(
        routing.get("adopt_active_model"),
        DEFAULTS["routing"]["adopt_active_model"],
    )
    if issue:
        warnings.append(f"routing.adopt_active_model: {issue}")

    api_key, issue = as_text(
        routing.get("api_key"),
        DEFAULTS["routing"]["api_key"],
    )
    if issue:
        warnings.append(f"routing.api_key: {issue}")

    restore, issue = as_bool(
        routing.get("restore_on_disable"),
        DEFAULTS["routing"]["restore_on_disable"],
    )
    if issue:
        warnings.append(f"routing.restore_on_disable: {issue}")

    state_file, issue = as_text(
        routing.get("state_file"),
        DEFAULTS["routing"]["state_file"],
    )
    if issue:
        warnings.append(f"routing.state_file: {issue}")

    return Settings(
        base_url=base_url,
        timeout_seconds=timeout,
        use_env_proxy=use_env_proxy,
        refresh_seconds=refresh,
        history_limit=history_limit,
        dashboard_path=dashboard_path,
        routing_enabled=enabled,
        provider_id=provider_id,
        provider_name=provider_name,
        api_path=api_path,
        chat_model=chat_model,
        model=model,
        adopt_active_model=adopt,
        api_key=api_key,
        restore_on_disable=restore,
        state_file=state_file,
        sources=dict(sources),
        warnings=tuple(dict.fromkeys(warnings)),
        config_path=str(config_path),
        config_found=config_found,
        config_error=config_error,
    )


def load_settings(
    *,
    plugin_dir: Optional[Path] = None,
    runtime_config: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> Settings:
    """Resolve the effective settings from every layer.

    Args:
        plugin_dir: Directory holding ``plugin.py``; the shipped
            ``headroom.config.json`` is looked up here.
        runtime_config: The ``plugins.headroom`` block from QwenPaw's config.
        environ: Process environment (injectable for tests).
        config_path: Explicit path to the plugin-local config file.
    """
    env = os.environ if environ is None else environ
    path = config_path or (
        (plugin_dir or Path(__file__).resolve().parent.parent) / CONFIG_FILENAME
    )
    file_payload, file_error = read_config_file(path)

    env_payload, env_origins = _env_layer(env)
    runtime_payload = _normalize_layer(dict(runtime_config or {}))
    file_layer = _normalize_layer(file_payload)

    merged = _deep_merge(DEFAULTS, file_layer)
    merged = _deep_merge(merged, runtime_payload)
    merged = _deep_merge(merged, env_payload)

    sources: dict[str, str] = {}
    for label, payload in (
        ("plugin-file", file_layer),
        ("qwenpaw-config", runtime_payload),
    ):
        for dotted in _collect_dotted(payload):
            sources[dotted] = label
    sources.update(env_origins)

    warnings: list[str] = []
    if file_error:
        warnings.append(f"config file: {file_error}")

    unknown: list[str] = []
    known_top = set(DEFAULTS)
    for key in file_payload:
        if key not in known_top and key not in FLAT_ALIASES and key not in KEY_MAP:
            unknown.append(f"plugin-file:{key}")
    for key in (runtime_config or {}):
        if key not in known_top and key not in FLAT_ALIASES and key not in KEY_MAP:
            unknown.append(f"config.json:{key}")
    if unknown:
        warnings.append("ignored unknown keys: " + ", ".join(sorted(unknown)))

    settings = _settings_from_merged(
        merged,
        sources,
        config_path=path,
        config_found=path.exists() and not file_error,
        config_error=file_error or "",
        extra_warnings=tuple(warnings),
    )
    LOGGER.debug(
        "[%s] settings resolved: base_url=%s (%s) routing=%s provider=%s",
        PLUGIN_ID,
        settings.base_url,
        settings.source("headroom.base_url"),
        settings.routing_enabled,
        settings.provider_id,
    )
    return settings


def with_overrides(settings: Settings, overrides: Mapping[str, Any]) -> Settings:
    """Return ``settings`` with a few attributes replaced.

    Accepts both attribute names (``base_url``) and the dotted keys used inside
    ``headroom.config.json`` (``headroom.base_url``); unknown keys are ignored.
    """
    fields = set(Settings.__dataclass_fields__)
    valid: dict[str, Any] = {}
    for key, value in (overrides or {}).items():
        target = FLAT_ALIASES.get(key, key)
        target = KEY_MAP.get(target, target)
        if target in fields:
            valid[target] = value
    return replace(settings, **valid) if valid else settings
def _unwrap_plugin_block(raw: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Accept either the plugin's own block or a whole host config."""
    plugins = raw.get("plugins")
    if isinstance(plugins, Mapping):
        block = plugins.get(PLUGIN_ID)
        return block if isinstance(block, Mapping) else None
    known = set(DEFAULTS) | set(FLAT_ALIASES) | set(KEY_MAP)
    return raw if any(key in known for key in raw) else None


def runtime_config_from_api(api: Any) -> dict[str, Any]:
    """Best-effort read of the plugin's own config block from the host."""
    raw = getattr(api, "config", None)
    if isinstance(raw, Mapping):
        block = _unwrap_plugin_block(raw)
        if block is not None:
            return {k: v for k, v in block.items() if k != "enabled"}
    try:  # pragma: no cover - depends on host internals
        from qwenpaw.config.utils import load_config  # pylint: disable=import-outside-toplevel

        root = load_config()
        plugins = getattr(root, "plugins", None)
        if isinstance(plugins, Mapping):
            block = plugins.get(PLUGIN_ID)
            if isinstance(block, Mapping):
                return {k: v for k, v in block.items() if k != "enabled"}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.debug("[%s] cannot read host config: %s", PLUGIN_ID, exc)
    return {}
