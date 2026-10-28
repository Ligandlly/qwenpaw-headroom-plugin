# -*- coding: utf-8 -*-
"""Point QwenPaw's model traffic at the Headroom proxy.

Headroom is a drop-in wire-compatible proxy: it speaks the OpenAI and
Anthropic HTTP dialects, forwards the client's own credentials upstream and
compresses the request body on the way through.  So "route through Headroom"
is exactly "register a provider whose ``base_url`` is the proxy and make it the
active chat provider".

Everything about the host is imported lazily and defensively: the plugin must
keep serving the savings dashboard even when the provider subsystem changes
between QwenPaw releases, and a rejected routing attempt is reported, never
raised out of a request handler.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

LOGGER = logging.getLogger(__name__)

STATE_FILENAME = "routing-state.json"
CHAT_SLOT = "chat"


class RoutingError(RuntimeError):
    """Routing could not be applied; ``kind`` explains why."""

    def __init__(self, message: str, *, kind: str = "routing-failed") -> None:
        super().__init__(message)
        self.kind = kind

    def as_dict(self) -> dict[str, Any]:
        return {"message": str(self), "kind": self.kind}


# ---------------------------------------------------------------------------
# Host access
# ---------------------------------------------------------------------------


def provider_manager() -> Any:
    """Return the host's provider manager singleton."""
    try:
        from qwenpaw.providers.provider_manager import ProviderManager
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise RoutingError(
            f"QwenPaw provider subsystem is unavailable: {exc}",
            kind="host-unavailable",
        ) from exc
    getter = getattr(ProviderManager, "get_instance", None)
    if callable(getter):
        return getter()
    return ProviderManager()


def state_dir() -> Path:
    try:
        from qwenpaw.constant import WORKING_DIR

        base = Path(WORKING_DIR)
    except Exception:  # pylint: disable=broad-exception-caught
        configured = os.environ.get("QWENPAW_WORKING_DIR")
        base = Path(configured).expanduser() if configured else Path("~/.qwenpaw").expanduser()
    return base / "plugin-data" / "headroom"


def read_state(settings: Any) -> dict[str, Any]:
    path = _state_path(settings)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOGGER.warning("[headroom] unreadable routing state %s: %s", path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(settings: Any, payload: Mapping[str, Any]) -> None:
    path = _state_path(settings)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(payload), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - disk issues
        LOGGER.warning("[headroom] cannot persist routing state %s: %s", path, exc)


def _state_path(settings: Any) -> Path:
    configured = str(getattr(settings, "state_file", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return state_dir() / STATE_FILENAME


def _slot_dict(slot: Any) -> dict[str, str]:
    if slot is None:
        return {}
    provider_id = getattr(slot, "provider_id", None)
    model = getattr(slot, "model", None)
    if provider_id is None and isinstance(slot, Mapping):
        provider_id = slot.get("provider_id")
        model = slot.get("model")
    return {
        "provider_id": str(provider_id or ""),
        "model": str(model or ""),
    }


def active_slot(manager: Any) -> dict[str, str]:
    getter = getattr(manager, "get_active_model", None)
    slot = None
    if callable(getter):
        try:
            slot = getter()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.debug("[headroom] get_active_model failed: %s", exc)
    if slot is None:
        slot = getattr(manager, "active_model", None)
    return _slot_dict(slot)


def find_provider(manager: Any, provider_id: str) -> Any:
    getter = getattr(manager, "get_provider", None)
    if not callable(getter):
        return None
    try:
        return getter(provider_id)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        LOGGER.debug("[headroom] get_provider(%s) failed: %s", provider_id, exc)
        return None


def provider_summary(provider: Any) -> dict[str, Any]:
    if provider is None:
        return {"exists": False}
    models = getattr(provider, "models", None) or []
    model_ids = [
        str(getattr(model, "id", model))
        for model in models
    ]
    return {
        "exists": True,
        "id": str(getattr(provider, "id", "")),
        "name": str(getattr(provider, "name", "")),
        "base_url": str(getattr(provider, "base_url", "")),
        "is_custom": bool(getattr(provider, "is_custom", False)),
        "chat_model": str(getattr(provider, "chat_model", "")),
        "api_key_set": bool(getattr(provider, "api_key", "")),
        "models": model_ids,
    }


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


@dataclass
class Target:
    """What the routing attempt is going to configure."""

    provider_id: str
    provider_name: str
    base_url: str
    api_key: str
    chat_model: str
    model: str
    adopted_from: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "base_url": self.base_url,
            "api_key_set": bool(self.api_key),
            "chat_model": self.chat_model,
            "model": self.model,
            "adopted_from": self.adopted_from,
        }


def resolve_target(manager: Any, settings: Any) -> Target:
    """Decide which model/credential the Headroom provider should carry.

    The user's current model is inherited by default (``adopt_active_model``):
    switching to a proxy must not also switch the model.  The API key is taken
    from the current provider because Headroom forwards the caller's
    credentials upstream unchanged.
    """
    slot = active_slot(manager)
    current = find_provider(manager, slot.get("provider_id", "")) if slot else None

    model = str(getattr(settings, "model", "") or "").strip()
    adopted_from = ""
    if not model and getattr(settings, "adopt_active_model", True):
        model = slot.get("model", "")
        if model:
            adopted_from = slot.get("provider_id", "")
    if not model:
        raise RoutingError(
            "no model selected: set routing.model in headroom.config.json "
            "or pick a model in the Console first",
            kind="no-model",
        )

    api_key = str(getattr(settings, "api_key", "") or "").strip()
    if not api_key:
        api_key = str(getattr(current, "api_key", "") or "")

    return Target(
        provider_id=str(settings.provider_id),
        provider_name=str(settings.provider_name),
        base_url=str(settings.api_base),
        api_key=api_key,
        chat_model=str(settings.chat_model or "OpenAIChatModel"),
        model=model,
        adopted_from=adopted_from,
    )


# ---------------------------------------------------------------------------
# Apply / disable
# ---------------------------------------------------------------------------


def _model_info(target: Target) -> Any:
    from qwenpaw.providers.model_info import ModelInfo

    return ModelInfo(
        id=target.model,
        name=target.model,
        model_type="chat",
        source="user",
    )


def _provider_info(target: Target) -> Any:
    from qwenpaw.providers.provider import ProviderInfo

    return ProviderInfo(
        id=target.provider_id,
        name=target.provider_name,
        base_url=target.base_url,
        api_key=target.api_key,
        chat_model=target.chat_model,
        models=[_model_info(target)],
        is_custom=True,
        discovery_strategy="unsupported",
        discovery_support_reason="Headroom proxy exposes the upstream model list",
    )


async def apply_routing(
    settings: Any,
    *,
    manager: Any = None,
) -> dict[str, Any]:
    """Create or refresh the Headroom provider and make it the chat default."""
    manager = manager or provider_manager()
    target = resolve_target(manager, settings)

    previous_slot = active_slot(manager)
    previous_provider = provider_summary(find_provider(manager, target.provider_id))
    state = read_state(settings)

    created = not previous_provider.get("exists", False)
    messages: list[str] = []

    if created:
        info = _provider_info(target)
        await _maybe_await(manager.add_custom_provider(info))
        messages.append(f"created provider {target.provider_id!r}")
    else:
        updates = {
            "base_url": target.base_url,
            "chat_model": target.chat_model,
            "models": [_model_info(target)],
        }
        if target.api_key:
            updates["api_key"] = target.api_key
        result = await _maybe_await(
            manager.update_provider_async(target.provider_id, updates),
        )
        if result is False:
            messages.append(
                f"provider {target.provider_id!r} rejected the update; "
                "kept the existing configuration",
            )
        else:
            messages.append(f"updated provider {target.provider_id!r}")

    changed_default, default_message = await _set_default_model(manager, target)
    if default_message:
        messages.append(default_message)

    if not state:
        state = {
            "previous_slot": previous_slot,
            "provider_existed": not created,
            "provider_snapshot": previous_provider,
            "first_applied_at": _now(),
        }
    state.update(
        {
            "provider_id": target.provider_id,
            "base_url": target.base_url,
            "model": target.model,
            "last_applied_at": _now(),
        },
    )
    write_state(settings, state)

    report = {
        "ok": True,
        "applied": True,
        "target": target.as_dict(),
        "created_provider": created,
        "default_model_changed": changed_default,
        "previous_slot": previous_slot,
        "messages": messages,
        "state": state,
        "status": routing_status(settings, manager=manager),
    }
    LOGGER.info(
        "[headroom] routing applied: provider=%s model=%s base_url=%s",
        target.provider_id,
        target.model,
        target.base_url,
    )
    return report


async def disable_routing(
    settings: Any,
    *,
    manager: Any = None,
) -> dict[str, Any]:
    """Undo :func:`apply_routing` as far as the recorded state allows."""
    manager = manager or provider_manager()
    state = read_state(settings)
    provider_id = str(state.get("provider_id") or settings.provider_id)
    messages: list[str] = []

    restore = bool(getattr(settings, "restore_on_disable", True))
    previous_slot = state.get("previous_slot") or {}
    if restore and previous_slot.get("model"):
        previous_provider = previous_slot.get("provider_id", "")
        exists = find_provider(manager, previous_provider) is not None
        if exists:
            await _maybe_await(
                _set_slot(manager, previous_provider, previous_slot["model"]),
            )
            messages.append(
                f"restored default model {previous_provider}/{previous_slot['model']}",
            )
        else:
            messages.append(
                f"previous provider {previous_provider!r} is gone; default left alone",
            )

    current = find_provider(manager, provider_id)
    if current is not None and not state.get("provider_existed", False):
        if getattr(current, "is_custom", False):
            remover = getattr(manager, "remove_custom_provider_async", None)
            if callable(remover):
                await _maybe_await(remover(provider_id))
            else:
                manager.remove_custom_provider(provider_id)
            messages.append(f"removed provider {provider_id!r}")
        else:
            messages.append(
                f"provider {provider_id!r} is built-in; left in place",
            )
    elif current is not None:
        messages.append(
            f"provider {provider_id!r} existed before the plugin; left in place",
        )

    state.update({"disabled_at": _now(), "applied": False})
    write_state(settings, state)
    LOGGER.info("[headroom] routing disabled: %s", "; ".join(messages) or "no changes")
    return {
        "ok": True,
        "applied": False,
        "restored": bool(restore and previous_slot.get("model")),
        "previous_slot": previous_slot,
        "messages": messages,
        "status": routing_status(settings, manager=manager),
    }


async def _set_default_model(manager: Any, target: Target) -> tuple[bool, str]:
    slot = _build_slot(target.provider_id, target.model)
    setter = getattr(manager, "set_default_model", None)
    if callable(setter):
        try:
            await _maybe_await(setter(CHAT_SLOT, slot))
            return True, f"default chat model -> {target.provider_id}/{target.model}"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.warning("[headroom] set_default_model failed: %s", exc)
    activator = getattr(manager, "activate_model", None)
    if callable(activator):
        try:
            await _maybe_await(activator(target.provider_id, target.model))
            return True, f"activated model {target.provider_id}/{target.model}"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise RoutingError(
                f"the host refused to activate the model: {exc}",
                kind="activate-failed",
            ) from exc
    messages = (
        f"provider {target.provider_id!r} is configured, but this QwenPaw build "
        "exposes no way to switch the default model automatically; "
        "select it in the Console model picker"
    )
    return False, messages


async def _set_slot(manager: Any, provider_id: str, model: str) -> Any:
    slot = _build_slot(provider_id, model)
    setter = getattr(manager, "set_default_model", None)
    if callable(setter):
        return await _maybe_await(setter(CHAT_SLOT, slot))
    return await _maybe_await(manager.activate_model(provider_id, model))


def _build_slot(provider_id: str, model: str) -> Any:
    from qwenpaw.config.config import ModelSlotConfig

    return ModelSlotConfig(provider_id=provider_id, model=model)


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def routing_status(settings: Any, *, manager: Any = None) -> dict[str, Any]:
    """Read-only view of how traffic is currently routed."""
    try:
        manager = manager or provider_manager()
    except RoutingError as exc:
        # Keep the same shape as the happy path so the page never has to care
        # whether QwenPaw's provider subsystem could be imported.
        return {
            "host_available": False,
            "enabled_in_config": bool(getattr(settings, "routing_enabled", False)),
            "provider_id": settings.provider_id,
            "expected_base_url": settings.api_base,
            "error": exc.as_dict(),
            "routed": False,
            "has_drift": False,
            "drift": [],
            "provider": {"exists": False},
            "active": {},
            "state": {},
            "state_file": str(_state_path(settings)),
        }

    slot = active_slot(manager)
    provider = find_provider(manager, settings.provider_id)
    summary = provider_summary(provider)
    state = read_state(settings)

    routed = bool(slot) and slot.get("provider_id") == settings.provider_id
    drift: list[str] = []
    if summary.get("exists"):
        configured = str(summary.get("base_url") or "").rstrip("/")
        expected = str(settings.api_base).rstrip("/")
        if configured != expected:
            drift.append(
                f"provider base_url {configured!r} != configured {expected!r}",
            )
    elif routed:  # pragma: no cover - inconsistent host state
        drift.append("active slot points at a provider that does not exist")

    return {
        "host_available": True,
        "enabled_in_config": bool(getattr(settings, "routing_enabled", False)),
        "provider_id": settings.provider_id,
        "expected_base_url": settings.api_base,
        "provider": summary,
        "active": slot,
        "routed": routed,
        "has_drift": bool(drift),
        "drift": drift,
        "state": state,
        "state_file": str(_state_path(settings)),
    }


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
