# -*- coding: utf-8 -*-
"""Routing tests — the host provider manager is faked, nothing touches QwenPaw."""

from __future__ import annotations

import asyncio

import pytest

from headroom_plugin import routing
from headroom_plugin.config import load_settings
from headroom_plugin.routing import (
    RoutingError,
    apply_routing,
    disable_routing,
    read_state,
    resolve_target,
    routing_status,
)
from tests.conftest import FakeManager, FakeProvider, FakeSlot


# ---------------------------------------------------------------------------
# Monkeypatched host objects (the real ones come from QwenPaw)
# ---------------------------------------------------------------------------

class FakeModelInfo:
    def __init__(self, model_id: str, name: str = "", model_type: str = "chat") -> None:
        self.id = model_id
        self.name = name or model_id
        self.model_type = model_type
        self.supports_multimodal = False
        self.is_free = False
        self.is_recommended = False
        self.source = "user"


class FakeProviderInfo:
    def __init__(self, **kwargs) -> None:
        self.id = kwargs.get("id", "")
        self.name = kwargs.get("name", "")
        self.base_url = kwargs.get("base_url", "")
        self.api_key = kwargs.get("api_key", "")
        self.chat_model = kwargs.get("chat_model", "OpenAIChatModel")
        self.models = list(kwargs.get("models", []))
        self.discovery_strategy = kwargs.get("discovery_strategy", "unsupported")


@pytest.fixture(autouse=True)
def fake_host_objects(monkeypatch):
    monkeypatch.setattr(routing, "_model_info", lambda target: FakeModelInfo(target.model))

    def build_provider_info(target):
        return FakeProviderInfo(
            id=target.provider_id,
            name=target.provider_name,
            base_url=target.base_url,
            api_key=target.api_key,
            chat_model=target.chat_model,
            models=[FakeModelInfo(target.model)],
        )

    monkeypatch.setattr(routing, "_provider_info", build_provider_info)
    monkeypatch.setattr(routing, "_build_slot", lambda provider_id, model: FakeSlot(provider_id, model))


def make_settings(tmp_path, **overrides):
    payload = {"state_file": str(tmp_path / "routing-state.json")}
    payload.update(overrides)
    return load_settings(plugin_dir=tmp_path, runtime_config=payload, environ={})


def make_manager(model: str = "qwen-max", api_key: str = "sk-real-key") -> FakeManager:
    upstream = FakeProvider("openai", base_url="https://api.example.com/v1", api_key=api_key)
    return FakeManager(providers={"openai": upstream}, active=FakeSlot("openai", model))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# resolve_target
# ---------------------------------------------------------------------------

def test_resolve_target_adopts_active_model(tmp_path):
    manager = make_manager()
    target = resolve_target(manager, make_settings(tmp_path))
    assert target.provider_id == "headroom"
    assert target.model == "qwen-max"
    assert target.adopted_from == "openai"
    assert target.api_key == "sk-real-key"
    assert target.base_url == "http://127.0.0.1:8787/v1"


def test_resolve_target_prefers_configured_model(tmp_path):
    manager = make_manager()
    target = resolve_target(manager, make_settings(tmp_path, model="qwen-plus", api_key="sk-explicit"))
    assert target.model == "qwen-plus"
    assert target.adopted_from == ""
    assert target.api_key == "sk-explicit"


def test_resolve_target_requires_a_model(tmp_path):
    manager = FakeManager(providers={}, active=None)
    with pytest.raises(RoutingError) as excinfo:
        resolve_target(manager, make_settings(tmp_path))
    assert excinfo.value.kind == "no-model"
    assert "model" in str(excinfo.value).lower()


def test_resolve_target_ignores_adoption_when_disabled(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path, adopt_active_model=False)
    with pytest.raises(RoutingError):
        resolve_target(manager, settings)


# ---------------------------------------------------------------------------
# apply_routing
# ---------------------------------------------------------------------------

def test_apply_creates_provider_and_switches_default(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path)
    report = run(apply_routing(settings, manager=manager))

    assert report["ok"] is True
    assert report["created_provider"] is True
    assert report["default_model_changed"] is True
    assert len(manager.added) == 1
    created = manager.added[0]
    assert created.id == "headroom"
    assert created.base_url == "http://127.0.0.1:8787/v1"
    assert created.api_key == "sk-real-key"

    assert manager.default_calls[-1][0] == "chat"
    slot = manager.default_calls[-1][1]
    assert (slot.provider_id, slot.model) == ("headroom", "qwen-max")
    assert manager.active.provider_id == "headroom"

    state = read_state(settings)
    assert state["provider_id"] == "headroom"
    assert state["previous_slot"] == {"provider_id": "openai", "model": "qwen-max"}
    assert state["provider_existed"] is False


def test_apply_is_idempotent_and_updates_existing_provider(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path)
    run(apply_routing(settings, manager=manager))
    report = run(apply_routing(settings, manager=manager))

    assert report["created_provider"] is False
    assert len(manager.added) == 1
    assert manager.updated
    provider_id, updates = manager.updated[-1]
    assert provider_id == "headroom"
    assert updates["base_url"] == "http://127.0.0.1:8787/v1"
    state = read_state(settings)
    assert state["previous_slot"] == {"provider_id": "openai", "model": "qwen-max"}


def test_apply_keeps_previous_slot_when_already_routed(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path)
    run(apply_routing(settings, manager=manager))
    run(apply_routing(settings, manager=manager))
    # The slot recorded for restore must stay the pre-Headroom one.
    assert read_state(settings)["previous_slot"] == {"provider_id": "openai", "model": "qwen-max"}


def test_apply_falls_back_to_activate_model(tmp_path):
    manager = make_manager()
    manager.set_default_model = None  # host without the async setter
    settings = make_settings(tmp_path)
    report = run(apply_routing(settings, manager=manager))
    assert report["ok"] is True
    assert manager.active.provider_id == "headroom"


def test_apply_reports_when_host_cannot_switch_default(tmp_path):
    manager = make_manager()
    manager.set_default_model = None
    manager.activate_model = None
    settings = make_settings(tmp_path)
    report = run(apply_routing(settings, manager=manager))
    assert report["default_model_changed"] is False
    assert any("model picker" in message for message in report["messages"])


# ---------------------------------------------------------------------------
# disable_routing
# ---------------------------------------------------------------------------

def test_disable_restores_previous_slot_and_removes_provider(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path)
    run(apply_routing(settings, manager=manager))
    report = run(disable_routing(settings, manager=manager))

    assert report["ok"] is True
    slot = manager.default_calls[-1][1]
    assert (slot.provider_id, slot.model) == ("openai", "qwen-max")
    assert "headroom" in manager.removed
    assert "headroom" not in manager.providers


def test_disable_keeps_provider_that_existed_before(tmp_path):
    manager = make_manager()
    manager.providers["headroom"] = FakeProvider("headroom", base_url="http://127.0.0.1:8787/v1")
    settings = make_settings(tmp_path)
    run(apply_routing(settings, manager=manager))
    run(disable_routing(settings, manager=manager))
    assert "headroom" not in manager.removed
    assert "headroom" in manager.providers


def test_disable_without_state_is_safe(tmp_path):
    manager = make_manager()
    report = run(disable_routing(make_settings(tmp_path), manager=manager))
    assert report["ok"] is True
    assert report["restored"] is False


# ---------------------------------------------------------------------------
# routing_status
# ---------------------------------------------------------------------------

def test_status_before_apply(tmp_path):
    manager = make_manager()
    status = routing_status(make_settings(tmp_path), manager=manager)
    assert status["host_available"] is True
    assert status["routed"] is False
    assert status["enabled_in_config"] is True
    assert status["expected_base_url"] == "http://127.0.0.1:8787/v1"
    assert status["provider"]["exists"] is False


def test_status_after_apply(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path)
    run(apply_routing(settings, manager=manager))
    status = routing_status(settings, manager=manager)
    assert status["routed"] is True
    assert status["has_drift"] is False
    assert status["drift"] == []
    assert status["provider"]["exists"] is True
    assert status["active"]["provider_id"] == "headroom"


def test_status_detects_drift(tmp_path):
    manager = make_manager()
    settings = make_settings(tmp_path)
    run(apply_routing(settings, manager=manager))
    manager.providers["headroom"].base_url = "http://127.0.0.1:9999/v1"
    status = routing_status(settings, manager=manager)
    assert status["has_drift"] is True
    assert "9999" in status["drift"][0]
    assert status["provider"]["base_url"] == "http://127.0.0.1:9999/v1"


def test_status_without_host(tmp_path):
    status = routing_status(make_settings(tmp_path), manager=None)
    assert status["host_available"] is False
    assert status["routed"] is False


def test_state_roundtrip_is_json(tmp_path):
    settings = make_settings(tmp_path)
    routing.write_state(settings, {"provider_id": "headroom", "model": "qwen-max"})
    assert read_state(settings) == {"provider_id": "headroom", "model": "qwen-max"}
    assert (tmp_path / "routing-state.json").exists()
