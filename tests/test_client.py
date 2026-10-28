# -*- coding: utf-8 -*-
"""HTTP client tests against a fake Headroom proxy (stdlib only)."""

from __future__ import annotations

import pytest

from headroom_plugin.client import HeadroomClient, HeadroomError
from tests.fixtures import HEALTH, HISTORY, LIFETIME, STATS


def make_client(base_url: str, **kwargs):
    kwargs.setdefault("timeout", 5.0)
    return HeadroomClient(base_url, **kwargs)


def test_health_roundtrip(headroom):
    client = make_client(headroom.base_url)
    assert client.health()["version"] == HEALTH["version"]


def test_stats_and_lifetime_roundtrip(headroom):
    client = make_client(headroom.base_url)
    assert client.stats()["summary"]["api_requests"] == STATS["summary"]["api_requests"]
    assert client.stats_lifetime()["scope"] == "lifetime"


def test_history_passes_limit(headroom):
    client = make_client(headroom.base_url)
    payload = client.stats_history(7)
    assert payload["schema_version"] == HISTORY["schema_version"]
    assert any("limit=7" in call for call in headroom.calls)


def test_snapshot_collects_everything(headroom):
    snapshot = make_client(headroom.base_url).snapshot(history_limit=3)
    assert snapshot["connected"] is True
    assert snapshot["base_url"] == headroom.base_url
    assert snapshot["health"]["version"] == HEALTH["version"]
    assert snapshot["stats"] is not None
    assert snapshot["lifetime"]["tokens"]["saved"] == LIFETIME["tokens"]["saved"]
    assert snapshot["history"]["display_session"]["tokens_saved"] == 40000
    assert snapshot["errors"] == {}
    assert snapshot["fetched_at"]


def test_snapshot_degrades_when_history_is_broken(headroom):
    headroom.statuses["/stats-history"] = 500
    snapshot = make_client(headroom.base_url).snapshot()
    assert snapshot["connected"] is True
    assert snapshot["history"] is None
    assert snapshot["errors"]["history"]["kind"] == "http"
    assert snapshot["errors"]["history"]["status"] == 500


def test_unreachable_proxy(fail_factory):
    client = make_client(f"http://127.0.0.1:{fail_factory()}", timeout=0.5)
    with pytest.raises(HeadroomError) as excinfo:
        client.health()
    assert excinfo.value.kind == "unreachable"
    error = excinfo.value.as_dict()
    assert error["kind"] == "unreachable"
    assert error["message"]


def test_snapshot_marks_disconnected_when_health_fails(fail_factory):
    client = make_client(f"http://127.0.0.1:{fail_factory()}", timeout=0.5)
    snapshot = client.snapshot()
    assert snapshot["connected"] is False
    assert snapshot.get("health") is None
    assert snapshot["error"]["kind"] == "unreachable"


def test_http_error_kind(headroom):
    headroom.statuses["/stats"] = 503
    with pytest.raises(HeadroomError) as excinfo:
        make_client(headroom.base_url).stats()
    assert excinfo.value.kind == "http"
    assert excinfo.value.status == 503


def test_bad_json_kind(headroom):
    headroom.payloads["/stats"] = "<html>not json</html>"
    with pytest.raises(HeadroomError) as excinfo:
        make_client(headroom.base_url).stats()
    assert excinfo.value.kind == "bad-json"


def test_bad_payload_kind(headroom):
    headroom.payloads["/stats"] = [1, 2, 3]
    with pytest.raises(HeadroomError) as excinfo:
        make_client(headroom.base_url).stats()
    assert excinfo.value.kind == "bad-payload"


def test_timeout_kind(headroom):
    headroom.delay = 0.4
    client = make_client(headroom.base_url, timeout=0.05)
    with pytest.raises(HeadroomError) as excinfo:
        client.health()
    assert excinfo.value.kind == "timeout"


def test_probe_reports_latency(headroom):
    probe = make_client(headroom.base_url).probe()
    assert probe["ok"] is True
    assert probe["version"] == HEALTH["version"]
    assert probe["mode"] == "cache"
    assert probe["uptime_seconds"] == HEALTH["uptime_seconds"]
    assert probe["latency_ms"] >= 0


def test_probe_reports_failure(fail_factory):
    probe = make_client(f"http://127.0.0.1:{fail_factory()}", timeout=0.4).probe()
    assert probe["ok"] is False
    assert probe["error"]["kind"] == "unreachable"


def test_base_url_trailing_slash_is_handled(headroom):
    client = make_client(headroom.base_url + "/")
    assert client.health()["status"] == "healthy"
    assert client.url_for("/health") == headroom.base_url + "/health"


def test_client_reads_settings(fail_factory, tmp_path):
    from headroom_plugin.client import client_from_settings
    from headroom_plugin.config import load_settings

    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"base_url": "http://127.0.0.1:1", "timeout_seconds": 2},
        environ={},
    )
    client = client_from_settings(settings)
    assert client.base_url == "http://127.0.0.1:1"
    assert client.timeout == 2.0
