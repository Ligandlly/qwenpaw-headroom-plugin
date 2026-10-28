# -*- coding: utf-8 -*-
"""Runtime tests: config reload, client/snapshot caching, routing actions."""

from __future__ import annotations

import asyncio
import json

import pytest

from headroom_plugin import runtime as runtime_module
from headroom_plugin.runtime import HeadroomRuntime, default_runtime_config


def write_config(tmp_path, base_url: str, **extra) -> None:
    payload = {"headroom": {"base_url": base_url, "timeout_seconds": 3}, "ui": {"history_limit": 5}}
    payload.update(extra)
    (tmp_path / "headroom.config.json").write_text(json.dumps(payload), encoding="utf-8")


def make_runtime(tmp_path, base_url, **kwargs) -> HeadroomRuntime:
    write_config(tmp_path, base_url)
    return HeadroomRuntime(plugin_dir=tmp_path, plugin_version="1.0.0", runtime_config=kwargs, environ={})


def test_settings_come_from_the_shipped_file(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url)
    settings = runtime.settings()
    assert settings.base_url == headroom.base_url
    assert settings.source("headroom.base_url") == "plugin-file"
    assert runtime.config_path.name == "headroom.config.json"


def test_runtime_config_overrides_the_file(tmp_path, headroom):
    write_config(tmp_path, "http://10.9.9.9:8787")
    runtime = HeadroomRuntime(
        plugin_dir=tmp_path,
        plugin_version="1.0.0",
        runtime_config={"base_url": headroom.base_url},
        environ={},
    )
    assert runtime.settings().base_url == headroom.base_url


def test_client_is_reused_until_the_url_changes(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url)
    first = runtime.client()
    assert runtime.client() is first

    runtime.update_runtime_config({"base_url": "http://127.0.0.1:9999"})
    second = runtime.client()
    assert second is not first
    assert second.base_url == "http://127.0.0.1:9999"


def test_snapshot_is_cached_then_refreshed(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url)
    first = runtime.snapshot()
    calls = len(headroom.calls)
    assert runtime.snapshot() is first
    assert len(headroom.calls) == calls

    refreshed = runtime.snapshot(force=True)
    assert refreshed is not first
    assert len(headroom.calls) > calls


def test_snapshot_honours_history_limit(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url)
    runtime.snapshot(history_limit=2)
    assert any("limit=2" in call for call in headroom.calls)


def test_overview_carries_plugin_metadata(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url)
    overview = runtime.overview()
    assert overview["plugin"]["id"] == "headroom"
    assert overview["plugin"]["version"] == "1.0.0"
    assert overview["headline"]["tokens_saved"] == 2163652
    assert overview["routing"]["host_available"] is False  # no QwenPaw in tests
    assert overview["config"]["base_url"] == headroom.base_url


def test_public_settings_are_browser_safe(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url, api_key="sk-inline-secret")
    public = runtime.public_settings()
    assert public["version"] == "1.0.0"
    assert "sk-inline-secret" not in json.dumps(public)
    assert public["routing"]["api_key_masked"]


def test_status_reports_connectivity(tmp_path, headroom):
    runtime = make_runtime(tmp_path, headroom.base_url)
    status = runtime.status()
    assert status["ok"] is True
    assert status["connected"] is True
    assert status["version"] == "0.42.0"
    assert status["base_url"] == headroom.base_url


def test_status_reports_failure_without_raising(tmp_path, fail_factory):
    runtime = make_runtime(tmp_path, f"http://127.0.0.1:{fail_factory()}")
    status = runtime.status()
    assert status["ok"] is False
    assert status["connected"] is False
    assert status["error"]["kind"] == "unreachable"


def test_apply_records_last_action(tmp_path, headroom, monkeypatch):
    runtime = make_runtime(tmp_path, headroom.base_url)

    async def fake_apply(settings, **_kwargs):
        return {"ok": True, "messages": ["created provider 'headroom'"], "target": {"provider_id": "headroom"}}

    monkeypatch.setattr(runtime_module, "apply_routing", fake_apply)
    report = asyncio.run(runtime.apply())
    assert report["ok"] is True
    last = runtime.overview()["last_action"]
    assert last["action"] == "apply"
    assert last["ok"] is True
    assert last["messages"] == ["created provider 'headroom'"]


def test_apply_surfaces_routing_errors(tmp_path, headroom, monkeypatch):
    from headroom_plugin.routing import RoutingError

    runtime = make_runtime(tmp_path, headroom.base_url)

    async def boom(settings, **_kwargs):
        raise RoutingError("no model to adopt", kind="no-model")

    monkeypatch.setattr(runtime_module, "apply_routing", boom)
    with pytest.raises(RoutingError):
        asyncio.run(runtime.apply())
    last = runtime.overview()["last_action"]
    assert last["ok"] is False
    assert last["error"]["kind"] == "no-model"


def test_disable_records_action(tmp_path, headroom, monkeypatch):
    runtime = make_runtime(tmp_path, headroom.base_url)

    async def fake_disable(settings, **_kwargs):
        return {"ok": True, "messages": ["restored default model openai/qwen-max"]}

    monkeypatch.setattr(runtime_module, "disable_routing", fake_disable)
    asyncio.run(runtime.disable())
    assert runtime.overview()["last_action"]["action"] == "disable"


def test_startup_skips_when_routing_disabled(tmp_path, headroom, monkeypatch):
    write_config(tmp_path, headroom.base_url, routing={"enabled": False})
    runtime = HeadroomRuntime(plugin_dir=tmp_path, plugin_version="1.0.0", runtime_config={}, environ={})
    called = []

    async def fake_apply(settings, **_kwargs):
        called.append(settings)
        return {}

    monkeypatch.setattr(runtime_module, "apply_routing", fake_apply)
    asyncio.run(runtime.apply_on_startup())
    assert called == []


def test_startup_swallows_failures(tmp_path, headroom, monkeypatch):
    runtime = make_runtime(tmp_path, headroom.base_url)

    async def boom(settings, **_kwargs):
        raise RuntimeError("proxy not up yet")

    monkeypatch.setattr(runtime_module, "apply_routing", boom)
    asyncio.run(runtime.apply_on_startup())  # must not raise


def test_startup_applies_when_enabled(tmp_path, headroom, monkeypatch):
    runtime = make_runtime(tmp_path, headroom.base_url)
    called = []

    async def fake_apply(settings, **_kwargs):
        called.append(settings)
        return {"target": {"provider_id": "headroom", "model": "qwen-max", "base_url": "http://x/v1"}}

    monkeypatch.setattr(runtime_module, "apply_routing", fake_apply)
    asyncio.run(runtime.apply_on_startup())
    assert len(called) == 1


def test_default_runtime_config_without_host():
    # No QwenPaw importable here, so the helper must degrade to an empty dict.
    assert default_runtime_config() == {}
