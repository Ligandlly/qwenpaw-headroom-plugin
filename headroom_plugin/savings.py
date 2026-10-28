# -*- coding: utf-8 -*-
"""Turn raw Headroom responses into one stable payload for the dashboard.

Headroom's ``/stats`` schema grows between releases, so every field is read
through :func:`dig` with a default.  A missing counter degrades one card, it
never raises: the dashboard is a monitoring surface and must stay useful when
the proxy is mid-upgrade.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Optional

MISSING = object()


def dig(payload: Any, path: str, default: Any = None) -> Any:
    """Read a dotted path out of nested mappings, returning ``default``."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return default
        current = current.get(part, MISSING)
        if current is MISSING:
            return default
    return default if current is None else current


def num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def as_int(value: Any, default: int = 0) -> int:
    return int(round(num(value, float(default))))


def as_float(value: Any, default: float = 0.0) -> float:
    return num(value, default)


def rounded(value: Any, digits: int = 2) -> float:
    return round(num(value), digits)


def _first_positive(*values: Any) -> float:
    for value in values:
        candidate = num(value, 0.0)
        if candidate:
            return candidate
    return 0.0


# ---------------------------------------------------------------------------
# Layers ("where did the savings come from")
# ---------------------------------------------------------------------------

LAYER_LABELS: dict[str, tuple[str, str]] = {
    "compression": ("上下文压缩", "Context compression"),
    "prefix_cache": ("前缀缓存", "Prefix cache"),
    "output_shaping": ("输出整形", "Output shaping"),
    "tool_search": ("工具检索", "Tool search"),
}


def build_layers(stats: Mapping[str, Any], tokens: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_layer = dig(stats, "savings.by_layer", {}) or {}
    cost_breakdown = dig(stats, "summary.cost.breakdown", {}) or {}
    compression = dig(stats, "summary.compression", {}) or {}

    def layer_section(key: str) -> Mapping[str, Any]:
        section = by_layer.get(key) if isinstance(by_layer, Mapping) else None
        return section if isinstance(section, Mapping) else {}

    rows: list[dict[str, Any]] = []

    section = layer_section("compression")
    tokens_saved = _first_positive(
        section.get("all_layers_tokens"),
        section.get("tokens"),
        compression.get("total_tokens_removed"),
        tokens.get("proxy_compression_saved"),
    )
    rows.append(
        {
            "key": "compression",
            "label_zh": LAYER_LABELS["compression"][0],
            "label_en": LAYER_LABELS["compression"][1],
            "available": bool(section) or tokens_saved > 0,
            "active": tokens_saved > 0,
            "unit": "tokens",
            "amount": tokens_saved,
            "tokens_saved": tokens_saved,
            "requests": as_int(compression.get("requests_compressed")),
            "usd_saved": as_float(
                cost_breakdown.get("compression_savings_usd"),
            ),
            "detail": "压缩后的请求上下文再次发送，节省的输入 token",
            "detail_en": "Input tokens removed from request context before sending",
        },
    )

    section = layer_section("prefix_cache")
    cache_usd = _first_positive(
        section.get("discount_usd"),
        cost_breakdown.get("cache_savings_usd"),
        dig(stats, "summary.cost.provider_cache_discount_usd"),
    )
    cache_tokens = _first_positive(section.get("tokens"))
    rows.append(
        {
            "key": "prefix_cache",
            "label_zh": LAYER_LABELS["prefix_cache"][0],
            "label_en": LAYER_LABELS["prefix_cache"][1],
            "available": bool(section) or cache_usd > 0,
            "active": cache_usd > 0 or cache_tokens > 0,
            "unit": "usd",
            "amount": cache_usd,
            "tokens_saved": cache_tokens,
            "requests": 0,
            "usd_saved": cache_usd,
            "detail": "上游供应商缓存命中带来的折扣（以金额计）",
            "detail_en": "Provider prompt-cache discount, measured in money",
        },
    )

    section = layer_section("output_shaping")
    shaping_tokens = _first_positive(section.get("tokens_saved"))
    rows.append(
        {
            "key": "output_shaping",
            "label_zh": LAYER_LABELS["output_shaping"][0],
            "label_en": LAYER_LABELS["output_shaping"][1],
            "available": bool(section.get("available", bool(section))),
            "active": bool(section.get("active")) or shaping_tokens > 0,
            "unit": "tokens",
            "amount": shaping_tokens,
            "tokens_saved": shaping_tokens,
            "requests": as_int(section.get("requests")),
            "usd_saved": 0.0,
            "reduction_percent": rounded(section.get("reduction_percent")),
            "detail": "输出长度约束（如 CI 区间）带来的节省",
            "detail_en": "Savings from constraining response verbosity",
        },
    )

    section = layer_section("tool_search")
    tool_tokens = _first_positive(
        section.get("tokens"),
        section.get("tokens_saved"),
        dig(stats, "summary.compression.tool_schema_tokens_saved"),
    )
    rows.append(
        {
            "key": "tool_search",
            "label_zh": LAYER_LABELS["tool_search"][0],
            "label_en": LAYER_LABELS["tool_search"][1],
            "available": bool(section) or tool_tokens > 0,
            "active": tool_tokens > 0,
            "unit": "tokens",
            "amount": tool_tokens,
            "tokens_saved": tool_tokens,
            "requests": as_int(section.get("requests")),
            "usd_saved": _first_positive(
                section.get("lifetime_usd"),
                cost_breakdown.get("tool_search_savings_usd"),
            ),
            "detail": "按需检索工具定义，而不是把全部 schema 塞进提示",
            "detail_en": "Tool schemas retrieved on demand instead of inlined",
        },
    )
    return rows


# ---------------------------------------------------------------------------
# Agents / models
# ---------------------------------------------------------------------------


def build_agents(stats: Mapping[str, Any], limit: int = 8) -> dict[str, Any]:
    usage = dig(stats, "agent_usage", {}) or {}
    raw_agents = usage.get("agents") if isinstance(usage, Mapping) else None
    agents: list[dict[str, Any]] = []
    if isinstance(raw_agents, Iterable) and not isinstance(raw_agents, (str, bytes)):
        for entry in raw_agents:
            if not isinstance(entry, Mapping):
                continue
            agents.append(
                {
                    "agent": str(entry.get("agent") or entry.get("label") or "unknown"),
                    "label": str(entry.get("label") or entry.get("agent") or "unknown"),
                    "requests": as_int(entry.get("requests")),
                    "before_tokens": as_int(entry.get("before_tokens")),
                    "after_tokens": as_int(entry.get("after_tokens")),
                    "output_tokens": as_int(entry.get("output_tokens")),
                    "tokens_saved": as_int(entry.get("tokens_saved")),
                    "savings_percent": rounded(entry.get("savings_percent")),
                    "share_of_saved_percent": rounded(
                        entry.get("share_of_saved_percent"),
                    ),
                    "models": entry.get("models") or [],
                    "exact": bool(entry.get("has_exact_tokens")),
                },
            )
    agents.sort(key=lambda item: item["tokens_saved"], reverse=True)
    totals = usage.get("totals") if isinstance(usage, Mapping) else None
    return {
        "rows": agents[:limit],
        "totals": {
            "requests": as_int((totals or {}).get("requests")),
            "tokens_saved": as_int((totals or {}).get("tokens_saved")),
            "savings_percent": rounded((totals or {}).get("savings_percent")),
        },
        "coverage": (usage.get("coverage") if isinstance(usage, Mapping) else None) or {},
    }


def build_models(stats: Mapping[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    by_model = dig(stats, "requests.by_model", {}) or {}
    if not isinstance(by_model, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for model, count in by_model.items():
        if isinstance(count, Mapping):
            requests = as_int(count.get("requests") or count.get("count"))
        else:
            requests = as_int(count)
        rows.append({"model": str(model), "requests": requests})
    rows.sort(key=lambda item: item["requests"], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def build_history(history: Optional[Mapping[str, Any]], limit: int) -> dict[str, Any]:
    if not isinstance(history, Mapping):
        return {"available": False, "points": [], "session": {}, "policy": {}}
    entries = history.get("history")
    if not isinstance(entries, list) and not history.get("schema_version") and not history.get(
        "display_session"
    ):
        return {"available": False, "points": [], "session": {}, "policy": {}, "lifetime": {}}
    points: list[dict[str, Any]] = []
    if isinstance(entries, Iterable) and not isinstance(entries, (str, bytes)):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            points.append(
                {
                    "timestamp": str(entry.get("timestamp") or ""),
                    "tokens_saved": as_int(entry.get("total_tokens_saved")),
                    "usd": rounded(entry.get("compression_savings_usd"), 6),
                    "model": str(entry.get("model") or ""),
                    "agent": str(entry.get("agent") or ""),
                },
            )
    points = points[-limit:] if limit > 0 else []
    session = history.get("display_session") or {}
    if not isinstance(session, Mapping):
        session = {}
    lifetime = history.get("lifetime") or {}
    if not isinstance(lifetime, Mapping):
        lifetime = {}
    return {
        "available": True,
        "points": points,
        "session": {
            "requests": as_int(session.get("requests")),
            "tokens_saved": as_int(session.get("tokens_saved")),
            "usd_saved": rounded(session.get("compression_savings_usd"), 6),
            "savings_percent": rounded(session.get("savings_percent")),
            "started_at": str(session.get("started_at") or ""),
            "last_activity_at": str(session.get("last_activity_at") or ""),
            "basis": str(session.get("savings_basis") or ""),
        },
        "lifetime": {
            "requests": as_int(lifetime.get("requests")),
            "tokens_saved": as_int(lifetime.get("tokens_saved")),
            "usd_saved": rounded(lifetime.get("compression_savings_usd"), 6),
            "input_tokens": as_int(lifetime.get("total_input_tokens")),
            "input_cost_usd": rounded(lifetime.get("total_input_cost_usd"), 4),
            "basis": str(lifetime.get("savings_basis") or ""),
        },
        "policy": history.get("display_session_policy") or {},
    }


# ---------------------------------------------------------------------------
# Disconnected hints
# ---------------------------------------------------------------------------

HINTS: dict[str, dict[str, str]] = {
    "unreachable": {
        "zh": "Headroom 代理没有响应。请确认它已启动，或修正配置里的 base_url。",
        "en": "The Headroom proxy did not answer. Start it, or fix base_url.",
        "command": "headroom proxy --host 127.0.0.1 --port 8787",
    },
    "timeout": {
        "zh": "连接 Headroom 超时。检查网络，或在配置中调大 timeout_seconds。",
        "en": "Connecting to Headroom timed out. Check the network or raise timeout_seconds.",
        "command": "headroom proxy --host 127.0.0.1 --port 8787",
    },
    "http": {
        "zh": "Headroom 返回了错误状态码。403/401 通常表示启用了 dashboard 访问口令。",
        "en": "Headroom returned an error status. 401/403 usually means a dashboard gate is enabled.",
        "command": "headroom proxy --help",
    },
    "bad-json": {
        "zh": "base_url 返回的不是 Headroom API，请确认端口指向 headroom proxy。",
        "en": "base_url is not serving the Headroom API — check the port.",
        "command": "curl -s http://127.0.0.1:8787/health",
    },
}


def hint_for(error: Optional[Mapping[str, Any]]) -> dict[str, str]:
    """Return the bilingual recovery hint for a client error, never raising."""
    kind = str((error or {}).get("kind") or "unreachable")
    if kind not in HINTS:
        kind = "unreachable"
    hint = dict(HINTS[kind])
    hint["kind"] = kind
    return hint


#: Zeroed headline used when the proxy is unreachable, so the page can render
#: the same component tree in both states.
EMPTY_HEADLINE: dict[str, Any] = {
    "tokens_saved": 0,
    "tokens_before": 0,
    "tokens_after": 0,
    "savings_percent": 0.0,
    "requests": 0,
    "compressed_requests": 0,
    "avg_compression_percent": 0.0,
    "best_compression_percent": 0.0,
    "best_detail": "",
    "cost_saved_usd": 0.0,
    "cost_without_usd": 0.0,
    "cost_with_usd": 0.0,
    "cost_savings_percent": 0.0,
    "cost_basis": "unavailable",
    "measured_usd": 0.0,
    "projected_usd": 0.0,
    "primary_model": "",
    "output_tokens": 0,
    "latency_ms": 0.0,
    "overhead_ms": 0.0,
    "mcp_compressions": 0,
    "mcp_tokens_removed": 0,
}


# ---------------------------------------------------------------------------
# Public payload
# ---------------------------------------------------------------------------


def build_overview(
    snapshot: Mapping[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Compose the single payload the dashboard renders."""
    payload: dict[str, Any] = {
        "plugin": {
            "id": getattr(settings, "plugin_id", "headroom"),
            "version": getattr(settings, "plugin_version", ""),
        },
        "config": settings.public(),
        "fetched_at": snapshot.get("fetched_at"),
        "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    if not snapshot.get("connected"):
        error = snapshot.get("error") or {}
        payload.update(
            {
                "connected": False,
                "error": error,
                "hint": hint_for(error),
                "base_url": snapshot.get("base_url"),
                "version": None,
                "status": "unreachable",
                "ready": False,
                "mode": None,
                "savings_profile": None,
                "uptime_seconds": 0,
                "headline": dict(EMPTY_HEADLINE),
                "lifetime": {"available": False},
                "layers": [],
                "cost_breakdown": {},
                "agents": {"rows": [], "totals": {}, "coverage": {}},
                "models": [],
                "requests": {"total": 0, "cached": 0, "failed": 0, "rate_limited": 0, "by_provider": {}},
                "history": {"available": False, "points": [], "session": {}, "policy": {}},
                "errors": snapshot.get("errors") or {},
            },
        )
        return payload

    health = snapshot.get("health") or {}
    stats = snapshot.get("stats") or {}
    lifetime_raw = snapshot.get("lifetime") or {}
    history_raw = snapshot.get("history") or {}

    summary = dig(stats, "summary", {}) or {}
    tokens = dig(stats, "tokens", {}) or {}
    requests = dig(stats, "requests", {}) or {}
    compression = dig(summary, "compression", {}) or {}
    cost = dig(summary, "cost", {}) or {}
    latency = dig(stats, "latency", {}) or {}
    health_config = dig(health, "config", {}) or {}

    tokens_saved = _first_positive(
        tokens.get("saved"),
        tokens.get("all_layers_saved"),
        compression.get("total_tokens_removed"),
    )
    tokens_before = _first_positive(
        tokens.get("total_before_compression"),
        tokens.get("attempted_input"),
        compression.get("total_tokens_before"),
    )
    if tokens_before < tokens_saved:
        tokens_before = tokens_saved + max(
            0.0,
            num(tokens.get("input")) or num(tokens.get("new_input_tokens")),
        )
    tokens_after = max(0.0, tokens_before - tokens_saved)
    savings_percent = _first_positive(
        tokens.get("all_layers_savings_percent"),
        tokens.get("savings_percent"),
    )
    if not savings_percent and tokens_before > 0:
        savings_percent = tokens_saved / tokens_before * 100.0

    breakdown = dig(cost, "breakdown", {}) or {}
    if not isinstance(breakdown, Mapping):
        breakdown = {}

    lifetime_payload = _build_lifetime(lifetime_raw, history_raw)

    # The proxy only prices a request it can map to a provider/model; when it
    # cannot (``savings_basis: unpriced``) the session cost reads 0.00 while the
    # lifetime rollup still carries a real number, so prefer the lifetime value
    # instead of showing the operator an empty money card.
    cost_saved = num(cost.get("total_saved_usd"))
    cost_without = num(cost.get("without_headroom_usd"))
    cost_with = num(cost.get("with_headroom_usd"))
    cost_percent = num(cost.get("savings_pct"))
    cost_basis = "session"
    if cost_saved <= 0:
        lifetime_saved = num(lifetime_payload.get("cost_saved_usd"))
        if lifetime_saved > 0:
            cost_saved = lifetime_saved
            cost_without = num(lifetime_payload.get("input_cost_usd")) or cost_without
            cost_with = max(0.0, cost_without - cost_saved)
            cost_percent = num(lifetime_payload.get("savings_percent"))
            cost_basis = "lifetime"
        else:
            cost_basis = "unavailable"

    payload.update(
        {
            "connected": True,
            "base_url": snapshot.get("base_url"),
            "version": health.get("version"),
            "status": health.get("status"),
            "ready": health.get("ready"),
            "mode": summary.get("mode") or health_config.get("mode"),
            "savings_profile": health_config.get("savings_profile"),
            "uptime_seconds": as_int(health.get("uptime_seconds")),
            "headline": {
                "tokens_saved": as_int(tokens_saved),
                "tokens_before": as_int(tokens_before),
                "tokens_after": as_int(tokens_after),
                "savings_percent": rounded(savings_percent),
                "requests": as_int(
                    _first_positive(
                        requests.get("total"),
                        summary.get("api_requests"),
                    ),
                ),
                "compressed_requests": as_int(compression.get("requests_compressed")),
                "avg_compression_percent": rounded(
                    compression.get("avg_compression_pct"),
                ),
                "best_compression_percent": rounded(
                    compression.get("best_compression_pct"),
                ),
                "best_detail": compression.get("best_detail") or "",
                "cost_saved_usd": rounded(cost_saved, 4),
                "cost_without_usd": rounded(cost_without, 4),
                "cost_with_usd": rounded(cost_with, 4),
                "cost_savings_percent": rounded(cost_percent),
                "cost_basis": cost_basis,
                "measured_usd": rounded(cost.get("measured_saved_usd"), 4),
                "projected_usd": rounded(cost.get("projected_saved_usd"), 4),
                "primary_model": summary.get("primary_model") or "",
                "output_tokens": as_int(tokens.get("output")),
                "latency_ms": rounded(latency.get("average_ms"), 1),
                "overhead_ms": rounded(latency.get("avg_overhead_ms"), 1),
                "mcp_compressions": as_int(dig(summary, "mcp.compressions")),
                "mcp_tokens_removed": as_int(dig(summary, "mcp.tokens_removed")),
            },
            "lifetime": lifetime_payload,
            "layers": build_layers(stats, tokens),
            "cost_breakdown": {
                "cache_savings_usd": rounded(breakdown.get("cache_savings_usd"), 4),
                "compression_savings_usd": rounded(
                    breakdown.get("compression_savings_usd"),
                    4,
                ),
                "compression_savings_list_usd": rounded(
                    breakdown.get("compression_savings_list_usd"),
                    4,
                ),
                "tool_search_savings_usd": rounded(
                    breakdown.get("tool_search_savings_usd"),
                    4,
                ),
                "provider_cache_discount_usd": rounded(
                    cost.get("provider_cache_discount_usd"),
                    4,
                ),
            },
            "agents": build_agents(stats),
            "models": build_models(stats),
            "requests": {
                "total": as_int(requests.get("total")),
                "cached": as_int(requests.get("cached")),
                "failed": as_int(requests.get("failed")),
                "rate_limited": as_int(requests.get("rate_limited")),
                "by_provider": requests.get("by_provider") or {},
            },
            "history": build_history(history_raw if isinstance(history_raw, Mapping) else None, getattr(settings, "history_limit", 60)),
            "errors": snapshot.get("errors") or {},
        },
    )
    return payload


def _build_lifetime(
    lifetime_raw: Mapping[str, Any],
    history_raw: Mapping[str, Any],
) -> dict[str, Any]:
    tokens = dig(lifetime_raw, "tokens", {}) or {}
    requests = dig(lifetime_raw, "requests", {}) or {}
    history_lifetime = dig(history_raw, "lifetime", {}) or {}
    saved = _first_positive(tokens.get("saved"), history_lifetime.get("tokens_saved"))
    before = _first_positive(
        tokens.get("attempted_input"),
        history_lifetime.get("total_input_tokens"),
    )
    input_tokens = _first_positive(tokens.get("input"))
    percent = _first_positive(
        tokens.get("token_savings_percent"),
    )
    if not percent and before > 0:
        percent = saved / before * 100.0
    return {
        "requests": as_int(_first_positive(requests.get("total"), history_lifetime.get("requests"))),
        "tokens_saved": as_int(saved),
        "tokens_before": as_int(before),
        "input_tokens": as_int(input_tokens),
        "savings_percent": rounded(percent),
        "cost_saved_usd": rounded(history_lifetime.get("compression_savings_usd"), 4),
        "input_cost_usd": rounded(history_lifetime.get("total_input_cost_usd"), 4),
        "started_at": str(lifetime_raw.get("started_at") or ""),
        "last_activity_at": str(lifetime_raw.get("last_activity_at") or ""),
        "basis": str(history_lifetime.get("savings_basis") or ""),
        "available": bool(lifetime_raw) or bool(history_lifetime),
    }
