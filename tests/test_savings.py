# -*- coding: utf-8 -*-
"""Savings shaping tests — pure functions over fixture payloads."""

from __future__ import annotations

import json

import pytest

from headroom_plugin.client import HeadroomClient
from headroom_plugin.config import load_settings
from headroom_plugin.savings import (
    HINTS,
    build_agents,
    build_history,
    build_layers,
    build_overview,
    hint_for,
)
from tests.fixtures import HEALTH, HISTORY, LIFETIME, STATS


def _settings(tmp_path, **overrides):
    return load_settings(plugin_dir=tmp_path, runtime_config=overrides, environ={})


def _snapshot(headroom, history_limit: int = 60):
    return HeadroomClient(headroom.base_url, timeout=5.0).snapshot(history_limit=history_limit)


def test_build_layers_reports_all_four(headroom, tmp_path):
    snapshot = _snapshot(headroom)
    layers = build_layers(snapshot["stats"], snapshot["stats"]["tokens"])
    keys = [layer["key"] for layer in layers]
    assert keys == ["compression", "prefix_cache", "output_shaping", "tool_search"]
    compression = layers[0]
    assert compression["tokens_saved"] == 2181994
    assert compression["active"] is True
    assert compression["label_zh"] and compression["label_en"]
    tool_search = next(layer for layer in layers if layer["key"] == "tool_search")
    assert tool_search["tokens_saved"] == 18342
    assert tool_search["unit"] == "tokens"


def test_build_agents_and_totals(headroom):
    stats = _snapshot(headroom)["stats"]
    agents = build_agents(stats)
    assert [row["agent"] for row in agents["rows"]] == ["main", "coder"]
    assert agents["rows"][0]["tokens_saved"] == 1500000
    assert agents["totals"]["requests"] == 440
    assert agents["coverage"]["mode"] == "mixed"


def test_build_history_keeps_points_and_session():
    history = build_history(HISTORY, 60)
    assert history["available"] is True
    assert len(history["points"]) == 3
    assert history["session"]["tokens_saved"] == 40000
    assert history["policy"]["rollover_inactivity_minutes"] == 60
    assert history["points"][-1]["tokens_saved"] == 900


def test_build_history_respects_limit():
    history = build_history(HISTORY, 2)
    assert len(history["points"]) == 2
    assert history["points"][0]["tokens_saved"] == 3400
    assert history["points"][-1]["tokens_saved"] == 900


def test_build_history_missing_payload():
    history = build_history({}, 60)
    assert history["available"] is False
    assert history["points"] == []


def test_build_overview_headline(headroom, tmp_path):
    overview = build_overview(_snapshot(headroom), _settings(tmp_path))
    assert overview["connected"] is True
    assert overview["version"] == HEALTH["version"]
    assert overview["mode"] == "cache"
    headline = overview["headline"]
    assert headline["tokens_saved"] == 2163652
    assert headline["tokens_before"] == 25999236
    assert headline["tokens_after"] == 23835584
    assert round(headline["savings_percent"], 2) == 8.39
    assert round(headline["cost_saved_usd"], 2) == 6.49
    assert round(headline["cost_without_usd"], 2) == 71.5
    assert headline["compressed_requests"] == 120
    assert headline["primary_model"] == "qwen-max"


def test_build_overview_sections(headroom, tmp_path):
    overview = build_overview(_snapshot(headroom), _settings(tmp_path))
    assert overview["lifetime"]["tokens_saved"] == 600000
    assert overview["requests"]["total"] == 440
    assert len(overview["agents"]["rows"]) == 2
    assert overview["history"]["available"] is True
    assert overview["errors"] == {}
    assert overview["config"]["base_url"] == "http://127.0.0.1:8787"


def test_build_overview_when_disconnected(fail_factory, tmp_path):
    client = HeadroomClient(f"http://127.0.0.1:{fail_factory()}", timeout=0.4)
    settings = _settings(tmp_path)
    overview = build_overview(client.snapshot(), settings)
    assert overview["connected"] is False
    assert overview["layers"] == []
    assert overview["agents"]["rows"] == []
    assert overview["headline"]["tokens_saved"] == 0
    hint = overview["hint"]
    assert hint["kind"] == "unreachable"
    assert hint["zh"] and hint["en"]
    assert "127.0.0.1" in overview["config"]["base_url"]


def test_hint_for_known_and_unknown_kinds():
    assert hint_for({"kind": "timeout"})["zh"] == HINTS["timeout"]["zh"]
    assert hint_for({"kind": "nope"})["kind"] == "unreachable"
    assert hint_for(None)["kind"] == "unreachable"


def test_overview_handles_empty_stats(tmp_path):
    snapshot = {
        "connected": True,
        "base_url": "http://127.0.0.1:8787",
        "health": {"version": "0.0.0"},
        "stats": {},
        "lifetime": LIFETIME,
        "history": None,
        "errors": {},
        "fetched_at": "2025-05-01T10:00:00Z",
    }
    overview = build_overview(snapshot, _settings(tmp_path))
    assert overview["connected"] is True
    assert overview["headline"]["tokens_saved"] == 0
    assert len(overview["layers"]) == 4
    assert all(layer["available"] is False for layer in overview["layers"])
    assert overview["history"]["available"] is False


def test_overview_flags_stale_proxy(tmp_path):
    snapshot = {
        "connected": True,
        "base_url": "http://10.1.2.3:8787",
        "health": {"version": "0.42.0", "ready": False, "status": "degraded"},
        "stats": STATS,
        "lifetime": LIFETIME,
        "history": {"history": HISTORY},
        "errors": {},
        "fetched_at": "2025-05-01T10:00:00Z",
    }
    overview = build_overview(snapshot, _settings(tmp_path))
    assert overview["ready"] is False
    assert overview["status"] == "degraded"


def _overview_snapshot(headroom, **stats_overrides):
    """A snapshot with a doctored /stats body."""
    stats = json.loads(json.dumps(STATS))
    for path, value in stats_overrides.items():
        node = stats
        parts = path.split("__")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    snapshot = HeadroomClient(headroom.base_url, timeout=5.0).snapshot(history_limit=60)
    snapshot["stats"] = stats
    return snapshot


def test_cost_falls_back_to_the_lifetime_rollup(headroom, tmp_path):
    """The proxy reports 0.00 for a session it cannot price; lifetime has the money."""
    snapshot = _overview_snapshot(
        headroom,
        summary__cost__total_saved_usd=0.0,
        summary__cost__without_headroom_usd=0.0,
        summary__cost__savings_pct=0.0,
    )
    headline = build_overview(snapshot, _settings(tmp_path))["headline"]
    assert headline["cost_basis"] == "lifetime"
    assert headline["cost_saved_usd"] > 0
    assert headline["cost_without_usd"] > headline["cost_saved_usd"]
    assert headline["cost_with_usd"] > 0


def test_cost_basis_is_session_when_the_proxy_prices_it(headroom, tmp_path):
    headline = build_overview(_snapshot(headroom), _settings(tmp_path))["headline"]
    assert headline["cost_basis"] == "session"
    assert headline["cost_saved_usd"] == pytest.approx(6.49, abs=1e-3)
