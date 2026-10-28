# -*- coding: utf-8 -*-
"""Configuration layering tests (no network, no QwenPaw import)."""

from __future__ import annotations

import json

import pytest

from headroom_plugin.config import (
    DEFAULTS,
    Settings,
    load_settings,
    normalize_url,
    parse_config_text,
    read_config_file,
    runtime_config_from_api,
    strip_json_comments,
    with_overrides,
)


def test_strip_json_comments_keeps_urls():
    text = """
    {
      // a line comment
      "headroom": {"base_url": "http://10.0.0.5:8787"}, /* trailing */
      "note": "keep // this inside a string"
    }
    """
    cleaned = strip_json_comments(text)
    assert "// a line comment" not in cleaned
    assert "trailing" not in cleaned
    assert "http://10.0.0.5:8787" in cleaned
    assert "keep // this inside a string" in cleaned
    assert json.loads(cleaned)["headroom"]["base_url"] == "http://10.0.0.5:8787"


def test_parse_config_text_rejects_garbage():
    with pytest.raises(Exception):
        parse_config_text("{ this is not json")


def test_read_config_file_reports_error(tmp_path):
    path = tmp_path / "headroom.config.json"
    path.write_text("{ broken", encoding="utf-8")
    payload, error = read_config_file(path)
    assert payload == {}
    assert error


def test_read_config_file_missing_is_silent(tmp_path):
    payload, error = read_config_file(tmp_path / "nope.json")
    assert payload == {}
    assert error is None


def test_defaults_only(tmp_path):
    settings = load_settings(plugin_dir=tmp_path, runtime_config={}, environ={})
    assert settings.base_url == DEFAULTS["headroom"]["base_url"]
    assert settings.routing_enabled is True
    assert settings.config_found is False
    assert settings.api_base == "http://127.0.0.1:8787/v1"
    assert settings.dashboard_url == "http://127.0.0.1:8787/dashboard"
    assert not settings.api_key_set


def test_file_layer_wins_over_defaults(tmp_path):
    (tmp_path / "headroom.config.json").write_text(
        """
        {
          // hardcoded for the offline bundle
          "headroom": {"base_url": "http://192.168.50.9:9999", "timeout_seconds": 9},
          "routing": {"model": "qwen-max"},
          "ui": {"refresh_seconds": 5}
        }
        """,
        encoding="utf-8",
    )
    settings = load_settings(plugin_dir=tmp_path, runtime_config={}, environ={})
    assert settings.base_url == "http://192.168.50.9:9999"
    assert settings.timeout_seconds == 9.0
    assert settings.model == "qwen-max"
    assert settings.refresh_seconds == 5
    assert settings.config_found is True
    assert settings.source("headroom.base_url") == "plugin-file"


def test_runtime_config_layer_wins_over_file(tmp_path):
    (tmp_path / "headroom.config.json").write_text(
        '{"headroom": {"base_url": "http://10.0.0.1:8787"}}', encoding="utf-8"
    )
    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"headroom_url": "http://localhost:1234"},
        environ={},
    )
    assert settings.base_url == "http://localhost:1234"
    assert settings.source("headroom.base_url") == "qwenpaw-config"


def test_env_layer_wins_over_runtime(tmp_path):
    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"base_url": "http://localhost:1234"},
        environ={"HEADROOM_BASE_URL": "http://env-host:7777", "HEADROOM_ROUTING_ENABLED": "0"},
    )
    assert settings.base_url == "http://env-host:7777"
    assert settings.routing_enabled is False
    assert settings.source("headroom.base_url") == "env:HEADROOM_BASE_URL"


def test_env_alias_is_used(tmp_path):
    settings = load_settings(
        plugin_dir=tmp_path, runtime_config={}, environ={"QWENPAW_HEADROOM_URL": "http://alias:8787"}
    )
    assert settings.base_url == "http://alias:8787"


def test_unknown_keys_are_reported_not_fatal(tmp_path):
    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"base_url": "http://x:1", "totally_unknown": 5},
        environ={},
    )
    assert settings.base_url == "http://x:1"
    assert any("totally_unknown" in warning for warning in settings.warnings)


def test_normalize_url():
    assert normalize_url("http://h:1/") == "http://h:1"
    assert normalize_url("http://h:1/v1/") == "http://h:1/v1"
    assert normalize_url("localhost:8787") == "http://localhost:8787"
    assert normalize_url("HTTPS://h:1/") == "https://h:1"
    with pytest.raises(Exception):
        normalize_url("   ")


def test_api_base_does_not_double_v1(tmp_path):
    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"base_url": "http://h:8787/v1", "api_path": "/v1"},
        environ={},
    )
    assert settings.api_base == "http://h:8787/v1"


def test_public_masks_api_key(tmp_path):
    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"api_key": "sk-super-secret-value"},
        environ={},
    )
    public = settings.public()
    assert "sk-super-secret-value" not in json.dumps(public)
    assert settings.api_key_set is True
    masked = settings.masked_api_key()
    assert "super-secret" not in masked
    assert masked.startswith("sk-s") and masked.endswith("alue")


def test_with_overrides_returns_new_object(tmp_path):
    base = load_settings(plugin_dir=tmp_path, runtime_config={}, environ={})
    changed = with_overrides(base, {"headroom.base_url": "http://other:1"})
    assert base.base_url != changed.base_url
    assert changed.base_url == "http://other:1"
    flat = with_overrides(base, {"base_url": "http://flat:2"})
    assert flat.base_url == "http://flat:2"
    assert with_overrides(base, {"nope": 1}) is base


def test_timeout_and_limits_are_clamped(tmp_path):
    settings = load_settings(
        plugin_dir=tmp_path,
        runtime_config={"timeout_seconds": -3, "history_limit": 100000, "refresh_seconds": 0},
        environ={},
    )
    assert settings.timeout_seconds == 0.5
    assert settings.history_limit == 500
    assert settings.refresh_seconds == 3


def test_runtime_config_from_api_prefers_plugins_block():
    class FakeApi:
        config = {"plugins": {"headroom": {"base_url": "http://from-api:8787"}}}

    assert runtime_config_from_api(FakeApi()) == {"base_url": "http://from-api:8787"}


def test_runtime_config_from_api_accepts_flat_block_and_deep_block():
    class Flat:
        config = {"base_url": "http://flat:1", "enabled": False}

    assert runtime_config_from_api(Flat()) == {"base_url": "http://flat:1"}

    class Deep:
        config = {"headroom": {"base_url": "http://deep:1"}}

    assert runtime_config_from_api(Deep()) == {"headroom": {"base_url": "http://deep:1"}}

    class Empty:
        config = None

    assert runtime_config_from_api(Empty()) == {}


def test_settings_is_frozen(tmp_path):
    settings = load_settings(plugin_dir=tmp_path, runtime_config={}, environ={})
    assert isinstance(settings, Settings)
    with pytest.raises(Exception):
        settings.base_url = "http://nope"  # type: ignore[misc]
