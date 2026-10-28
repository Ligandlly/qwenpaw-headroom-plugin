# -*- coding: utf-8 -*-
"""Router tests.  Skipped unless FastAPI is importable (i.e. inside QwenPaw's venv)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="the plugin's HTTP routes need FastAPI (QwenPaw venv)")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from headroom_plugin import runtime as runtime_module  # noqa: E402
from headroom_plugin.routes import build_router  # noqa: E402
from headroom_plugin.routing import RoutingError  # noqa: E402
from headroom_plugin.runtime import HeadroomRuntime  # noqa: E402


def make_client(tmp_path, base_url) -> TestClient:
    (tmp_path / "headroom.config.json").write_text(
        json.dumps({"headroom": {"base_url": base_url, "timeout_seconds": 2}}),
        encoding="utf-8",
    )
    runtime = HeadroomRuntime(plugin_dir=tmp_path, plugin_version="1.0.0", runtime_config={}, environ={})
    app = FastAPI()
    app.include_router(build_router(runtime), prefix="/headroom")
    return TestClient(app)


def test_health_endpoint(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["plugin"]["id"] == "headroom"
    assert body["proxy"]["ok"] is True
    assert body["proxy"]["version"] == "0.42.0"
    assert body["config"]["base_url"] == headroom.base_url


def test_config_endpoint(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/config")
    assert response.status_code == 200
    body = response.json()
    assert body["config"]["base_url"] == headroom.base_url
    assert body["config"]["api_base"] == f"{headroom.base_url}/v1"
    assert body["routing"]["routing"]["provider_id"] == "headroom"


def test_config_reload_endpoint(tmp_path, headroom):
    client = make_client(tmp_path, headroom.base_url)
    response = client.post("/headroom/config/reload")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_savings_endpoint(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/savings")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["headline"]["tokens_saved"] == 2163652
    assert len(body["layers"]) == 4
    assert body["proxy"]["ok"] is True


def test_savings_endpoint_passes_history_limit(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/savings?history_limit=2&force=true")
    assert response.status_code == 200
    assert any("limit=2" in call for call in headroom.calls)


def test_savings_endpoint_when_proxy_is_down(tmp_path, fail_factory):
    response = make_client(tmp_path, f"http://127.0.0.1:{fail_factory()}").get("/headroom/savings")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["hint"]["kind"] == "unreachable"
    assert body["headline"]["tokens_saved"] == 0


def test_stats_endpoint(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["snapshot"]["health"]["version"] == "0.42.0"


def test_probe_endpoint(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).post("/headroom/probe")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["probe"]["ok"] is True
    assert body["probe"]["latency_ms"] >= 0


def test_probe_endpoint_reports_failure(tmp_path, fail_factory):
    response = make_client(tmp_path, f"http://127.0.0.1:{fail_factory()}").post("/headroom/probe")
    assert response.status_code == 200
    assert response.json()["probe"]["ok"] is False


def test_routing_endpoint_without_host(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/routing")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    # `host_available` flips to True when the tests run inside a venv that has
    # QwenPaw installed, so only require the key (and the flat routing payload).
    assert "host_available" in body["routing"]
    assert body["routing"]["provider_id"] == "headroom"
    assert body["proxy"]["connected"] is True


def test_routing_apply_conflict_is_409(tmp_path, headroom, monkeypatch):
    client = make_client(tmp_path, headroom.base_url)

    async def boom(self):
        raise RoutingError("no model to adopt", kind="no-model")

    monkeypatch.setattr(runtime_module.HeadroomRuntime, "apply", boom)
    response = client.post("/headroom/routing/apply")
    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["kind"] == "no-model"


def test_routing_apply_success(tmp_path, headroom, monkeypatch):
    client = make_client(tmp_path, headroom.base_url)

    async def fake_apply(self):
        return {"applied": True, "target": {"provider_id": "headroom", "model": "qwen-max"}}

    monkeypatch.setattr(runtime_module.HeadroomRuntime, "apply", fake_apply)
    response = client.post("/headroom/routing/apply")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["applied"] is True


def test_routing_disable_success(tmp_path, headroom, monkeypatch):
    client = make_client(tmp_path, headroom.base_url)

    async def fake_disable(self):
        return {"applied": False, "restored": True, "messages": ["restored"]}

    monkeypatch.setattr(runtime_module.HeadroomRuntime, "disable", fake_disable)
    response = client.post("/headroom/routing/disable")
    assert response.status_code == 200
    assert response.json()["restored"] is True


def test_settings_preview_degrades(tmp_path, headroom):
    response = make_client(tmp_path, headroom.base_url).get("/headroom/settings/preview")
    body = response.json()
    if response.status_code == 200:
        assert body["ok"] is True
        assert body["target"]["provider_id"] == "headroom"
    else:
        # No QwenPaw provider subsystem, or no model to adopt yet.
        assert response.status_code in (409, 500, 503)
        assert body["ok"] is False
        assert body["kind"] in {"host-unavailable", "no-model", "unexpected"}
