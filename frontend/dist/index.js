// @ts-nocheck
/**
 * Headroom savings page — plain ES module, no build step.
 *
 * Loaded by the Console as /api/frontend_plugin/headroom/files/dist/index.js.
 * Everything comes from window.QwenPaw: React/antd are host externals, and
 * host.fetch() already prefixes /api and injects the auth headers.
 *
 * The page only reads /api/headroom/*; it never talks to the Headroom proxy
 * directly, so the proxy URL stays a server-side concern (headroom.config.json).
 */

const PLUGIN_ID = "headroom";
const ROUTE_ID = "headroom.savings";
const ROUTE_PATH = "/headroom";
const MENU_ID = "headroom.menu";
const API_ROOT = "/headroom";
const REQUEST_TIMEOUT_MS = 20000;

const ZH = /^zh/i.test(
  (typeof navigator !== "undefined" && (navigator.language || navigator.userLanguage)) || "zh",
);

/** Bilingual string helper: t("中文", "English"). */
function t(zh, en) {
  return ZH ? zh : en;
}

// ---------------------------------------------------------------------------
// Data access
// ---------------------------------------------------------------------------

let hostRef = null;

function apiUrl(path) {
  if (hostRef && typeof hostRef.getApiUrl === "function") {
    try {
      return hostRef.getApiUrl(path);
    } catch (err) {
      /* fall through to the /api prefix below */
    }
  }
  return `/api${path}`;
}

function authHeaders() {
  const headers = { accept: "application/json" };
  try {
    if (hostRef && typeof hostRef.getApiToken === "function") {
      const token = hostRef.getApiToken();
      if (token) headers.Authorization = `Bearer ${token}`;
    }
    if (hostRef && typeof hostRef.getSelectedAgentId === "function") {
      const agent = hostRef.getSelectedAgentId();
      if (agent) headers["X-Agent-Id"] = agent;
    }
  } catch (err) {
    /* headers are best effort */
  }
  return headers;
}

async function request(path, init) {
  const options = Object.assign({ method: "GET", credentials: "same-origin" }, init || {});
  options.headers = Object.assign(authHeaders(), (init && init.headers) || {});
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  options.signal = controller.signal;
  try {
    const response = await fetch(apiUrl(`${API_ROOT}${path}`), options);
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch (err) {
        payload = { ok: false, message: text.slice(0, 400) };
      }
    }
    if (!response.ok) {
      const message =
        (payload && (payload.message || payload.detail)) || `HTTP ${response.status}`;
      const error = new Error(message);
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload || {};
  } finally {
    clearTimeout(timer);
  }
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function isNum(value) {
  return typeof value === "number" && isFinite(value);
}

function fmtInt(value) {
  if (!isNum(value)) return "0";
  return Math.round(value).toLocaleString("en-US");
}

function fmtCompact(value) {
  if (!isNum(value)) return "0";
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e4) return `${(value / 1e3).toFixed(1)}K`;
  return fmtInt(value);
}

function fmtPct(value, digits) {
  if (!isNum(value)) return "0%";
  const places = typeof digits === "number" ? digits : value < 10 ? 2 : 1;
  return `${value.toFixed(places)}%`;
}

function fmtUsd(value) {
  if (!isNum(value)) return "$0.00";
  if (value > 0 && value < 0.01) return `$${value.toFixed(4)}`;
  if (value >= 1000) return `$${fmtCompact(value)}`;
  return `$${value.toFixed(2)}`;
}

/**
 * Explains the money card: the proxy only prices a request it can map to a
 * provider/model, so a session total of 0.00 can mean "priced elsewhere".
 */
function costHint(headline) {
  if (!headline) return "—";
  if (headline.cost_basis === "unavailable") {
    return t("代理未对该模型计价", "The proxy has no pricing for this model");
  }
  const range = `${fmtUsd(headline.cost_without_usd)} → ${fmtUsd(headline.cost_with_usd)}`;
  if (headline.cost_basis === "lifetime") {
    return t(`${range}（累计口径）`, `${range} (lifetime)`);
  }
  if (headline.cost_without_usd) return range;
  return t("暂无成本数据", "No cost data");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (isNaN(date.getTime())) return String(iso);
  return date.toLocaleTimeString(ZH ? "zh-CN" : "en-US", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtUptime(seconds) {
  if (!isNum(seconds) || seconds <= 0) return "—";
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return t(`${days} 天 ${hours} 小时`, `${days}d ${hours}h`);
  if (hours > 0) return t(`${hours} 小时 ${minutes} 分`, `${hours}h ${minutes}m`);
  return t(`${minutes} 分钟`, `${minutes}m`);
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function el(React, type, props, ...children) {
  return React.createElement(type, props, ...children);
}

function Panel(props) {
  const { React } = props;
  return el(
    React,
    "section",
    { className: `hr-panel${props.className ? ` ${props.className}` : ""}` },
    el(
      React,
      "header",
      { className: "hr-panel-head" },
      el(React, "h2", { className: "hr-panel-title" }, props.title),
      props.subtitle ? el(React, "span", { className: "hr-panel-sub" }, props.subtitle) : null,
      props.extra || null,
    ),
    el(React, "div", { className: "hr-panel-body" }, props.children),
  );
}

function KpiCard(props) {
  const { React } = props;
  return el(
    React,
    "article",
    { className: `hr-kpi${props.accent ? " hr-kpi-accent" : ""}` },
    el(React, "div", { className: "hr-kpi-label" }, props.label),
    el(
      React,
      "div",
      { className: "hr-kpi-value", title: props.title || "" },
      props.value,
      props.unit ? el(React, "span", { className: "hr-kpi-unit" }, props.unit) : null,
    ),
    props.hint ? el(React, "div", { className: "hr-kpi-hint" }, props.hint) : null,
  );
}

function Sparkline(props) {
  const { React, points } = props;
  if (!points.length) {
    return el(React, "div", { className: "hr-empty" }, t("暂无请求记录", "No requests yet"));
  }
  const width = 320;
  const height = 72;
  const values = points.map((point) => (isNum(point.tokens_saved) ? point.tokens_saved : 0));
  const max = Math.max.apply(null, values.concat([1]));
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const coords = values.map((value, index) => {
    const x = values.length > 1 ? index * step : width / 2;
    const y = height - (value / max) * (height - 6) - 3;
    return [x, y];
  });
  const line = coords.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${width},${height} L0,${height} Z`;
  const last = coords[coords.length - 1] || [0, height];

  return el(
    React,
    "div",
    { className: "hr-spark" },
    el(
      React,
      "svg",
      {
        viewBox: `0 0 ${width} ${height}`,
        preserveAspectRatio: "none",
        className: "hr-spark-svg",
        role: "img",
        "aria-label": t("每次请求节省的 token 曲线", "Tokens saved per request"),
      },
      el(React, "defs", null,
        el(
          React,
          "linearGradient",
          { id: "hr-spark-fill", x1: "0", y1: "0", x2: "0", y2: "1" },
          el(React, "stop", { offset: "0%", stopColor: "currentColor", stopOpacity: "0.38" }),
          el(React, "stop", { offset: "100%", stopColor: "currentColor", stopOpacity: "0.02" }),
        ),
      ),
      el(React, "path", { d: area, fill: "url(#hr-spark-fill)", stroke: "none" }),
      el(React, "path", {
        d: line,
        fill: "none",
        stroke: "currentColor",
        strokeWidth: "1.5",
        strokeLinejoin: "round",
        strokeLinecap: "round",
        vectorEffect: "non-scaling-stroke",
      }),
      el(React, "circle", {
        cx: last[0].toFixed(1),
        cy: last[1].toFixed(1),
        r: "2.4",
        fill: "currentColor",
      }),
    ),
    el(
      React,
      "div",
      { className: "hr-spark-scale" },
      el(React, "span", null, fmtCompact(max)),
      el(React, "span", null, t("最近 ", "latest "), fmtTime(points[points.length - 1].timestamp)),
    ),
  );
}

function Bar(props) {
  const { React } = props;
  const share = isNum(props.share) ? Math.max(0, Math.min(100, props.share)) : 0;
  return el(
    React,
    "div",
    { className: `hr-bar-row${props.active ? "" : " hr-bar-row-idle"}` },
    el(
      React,
      "div",
      { className: "hr-bar-head" },
      el(React, "span", { className: "hr-bar-label" }, props.label),
      el(React, "span", { className: "hr-bar-value" }, props.value),
    ),
    el(
      React,
      "div",
      { className: "hr-bar-track" },
      el(React, "div", {
        className: "hr-bar-fill",
        style: { width: `${share}%` },
      }),
    ),
    props.detail ? el(React, "div", { className: "hr-bar-detail" }, props.detail) : null,
  );
}

function Row(props) {
  const { React } = props;
  return el(
    React,
    "div",
    { className: "hr-row" },
    el(React, "span", { className: "hr-row-key" }, props.label),
    el(React, "span", { className: `hr-row-value${props.mono ? " hr-mono" : ""}` }, props.value),
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

function HeadroomPage(props) {
  const { React } = props;
  const { useCallback, useEffect, useMemo, useRef, useState } = React;

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState(null);
  const [tick, setTick] = useState(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const load = useCallback(
    async (options) => {
      const force = options && options.force;
      if (force) setLoading(true);
      try {
        const payload = await request(`/savings${force ? "?force=true" : ""}`);
        if (!mounted.current) return;
        setData(payload);
        setError(null);
      } catch (err) {
        if (!mounted.current) return;
        setError(err);
      } finally {
        if (mounted.current) setLoading(false);
      }
    },
    [],
  );

  const refreshSeconds = (data && data.config && data.config.ui && data.config.ui.refresh_seconds) || 20;

  useEffect(() => {
    load({ force: true });
  }, [load]);

  useEffect(() => {
    const interval = Math.max(3, Number(refreshSeconds) || 20) * 1000;
    const handle = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      load();
    }, interval);
    return () => clearInterval(handle);
  }, [load, refreshSeconds, tick]);

  const run = useCallback(
    async (label, path, method) => {
      setBusy(label);
      setNote(null);
      try {
        const payload = await request(path, { method: method || "POST" });
        const messages = (payload && payload.messages) || [];
        setNote({
          kind: "ok",
          text: messages.length ? messages.join("；") : t("完成", "Done"),
        });
      } catch (err) {
        setNote({
          kind: "error",
          text: (err && err.message) || t("操作失败", "Action failed"),
        });
      } finally {
        if (mounted.current) setBusy("");
        load({ force: true });
      }
    },
    [load],
  );

  const config = (data && data.config) || {};
  const routing = (data && data.routing) || {};
  const headline = (data && data.headline) || null;
  const lifetime = (data && data.lifetime) || {};
  const history = (data && data.history) || { points: [] };
  const agents = (data && data.agents) || { rows: [], totals: {} };
  const layers = (data && data.layers) || [];
  const connected = Boolean(data && data.connected);
  const hint = (data && data.hint) || null;
  const proxyError = (data && data.error) || null;

  const layerTotal = useMemo(
    () =>
      layers.reduce((sum, layer) => {
        if (layer.unit === "usd") return sum;
        const amount = isNum(layer.amount) ? layer.amount : 0;
        return sum + amount;
      }, 0),
    [layers],
  );

  const handleRefresh = useCallback(() => {
    setTick((value) => value + 1);
    load({ force: true });
  }, [load]);

  const dashboardUrl = config.dashboard_url || "";
  const openDashboard = useCallback(() => {
    if (!dashboardUrl) return;
    if (hostRef && typeof hostRef.openExternalLink === "function") {
      hostRef.openExternalLink(dashboardUrl);
    } else if (typeof window !== "undefined") {
      window.open(dashboardUrl, "_blank", "noopener");
    }
  }, [dashboardUrl]);

  const statusTag = connected
    ? el(React, "span", { className: "hr-badge hr-badge-ok" }, t("已连接", "Connected"))
    : el(React, "span", { className: "hr-badge hr-badge-off" }, t("未连接", "Offline"));

  const header = el(
    React,
    "header",
    { className: "hr-header" },
    el(
      React,
      "div",
      { className: "hr-heading" },
      el(
        React,
        "h1",
        { className: "hr-title" },
        t("Headroom 上下文压缩", "Headroom Context Compression"),
      ),
      el(
        React,
        "p",
        { className: "hr-subtitle" },
        statusTag,
        el(React, "span", { className: "hr-mono" }, config.base_url || "—"),
        connected && data.version ? el(React, "span", null, `v${data.version}`) : null,
        connected ? el(React, "span", null, t("模式 ", "mode "), data.mode || "—") : null,
        connected ? el(React, "span", null, t("在线 ", "up "), fmtUptime(data.uptime_seconds)) : null,
      ),
    ),
    el(
      React,
      "div",
      { className: "hr-actions" },
      data && data.fetched_at
        ? el(React, "span", { className: "hr-updated" }, t("更新于 ", "updated "), fmtTime(data.fetched_at))
        : null,
      el(
        React,
        "button",
        {
          type: "button",
          className: "hr-btn",
          onClick: handleRefresh,
          disabled: loading,
        },
        loading ? t("刷新中…", "Refreshing…") : t("刷新", "Refresh"),
      ),
      dashboardUrl
        ? el(
            React,
            "button",
            { type: "button", className: "hr-btn hr-btn-ghost", onClick: openDashboard },
            t("打开仪表盘", "Open dashboard"),
          )
        : null,
    ),
  );

  if (!data && loading) {
    return el(
      React,
      "div",
      { className: "hr-root" },
      header,
      el(React, "div", { className: "hr-skeleton" }, t("正在读取 Headroom 数据…", "Loading Headroom data…")),
    );
  }

  if (!connected) {
    return el(
      React,
      "div",
      { className: "hr-root" },
      header,
      el(
        React,
        "section",
        { className: "hr-panel hr-alert" },
        el(React, "h2", { className: "hr-alert-title" }, t("无法连接 Headroom 代理", "Cannot reach the Headroom proxy")),
        el(
          React,
          "p",
          { className: "hr-alert-text" },
          (proxyError && proxyError.message) || (error && error.message) || t("未知错误", "Unknown error"),
        ),
        hint
          ? el(
              React,
              "p",
              { className: "hr-alert-text hr-muted" },
              ZH ? hint.zh : hint.en,
            )
          : null,
        hint && hint.command
          ? el(
              React,
              "pre",
              { className: "hr-code" },
              `${hint.command}\n# ${t("或修改配置里的 base_url：", "or set base_url in")} ${config.config_file
                ? config.config_file.path
                : "headroom.config.json"}`,
            )
          : null,
        el(
          React,
          "div",
          { className: "hr-actions" },
          el(
            React,
            "button",
            { type: "button", className: "hr-btn", onClick: handleRefresh, disabled: loading },
            t("重试", "Retry"),
          ),
          el(
            React,
            "button",
            {
              type: "button",
              className: "hr-btn hr-btn-ghost",
              onClick: () => run(t("重载配置", "Reload config"), "/config/reload", "POST"),
              disabled: busy === t("重载配置", "Reload config"),
            },
            t("重载配置", "Reload config"),
          ),
        ),
        note
          ? el(
              React,
              "p",
              { className: `hr-note hr-note-${note.kind}` },
              note.text,
            )
          : null,
      ),
    );
  }

  const kpis = el(
    React,
    "section",
    { className: "hr-kpis" },
    el(KpiCard, {
      React,
      accent: true,
      label: t("本次节省 Token", "Tokens saved"),
      value: fmtCompact(headline.tokens_saved),
      title: `${fmtInt(headline.tokens_saved)} tokens`,
      hint: `${fmtPct(headline.savings_percent)} · ${fmtCompact(headline.tokens_before)} → ${fmtCompact(
        headline.tokens_after,
      )}`,
    }),
    el(KpiCard, {
      React,
      label: t("节省金额", "Cost saved"),
      value: headline.cost_basis === "unavailable" ? "—" : fmtUsd(headline.cost_saved_usd),
      hint: costHint(headline),
    }),
    el(KpiCard, {
      React,
      label: t("压缩请求", "Compressed requests"),
      value: fmtInt(headline.compressed_requests),
      hint: t(
        `平均压缩 ${fmtPct(headline.avg_compression_percent)} · 最佳 ${fmtPct(headline.best_compression_percent)}`,
        `avg ${fmtPct(headline.avg_compression_percent)} · best ${fmtPct(headline.best_compression_percent)}`,
      ),
    }),
    el(KpiCard, {
      React,
      label: t("请求 / 延迟", "Requests / latency"),
      value: fmtInt(headline.requests),
      hint: t(
        `平均 ${headline.latency_ms || 0}ms · 代理开销 ${headline.overhead_ms || 0}ms`,
        `avg ${headline.latency_ms || 0}ms · overhead ${headline.overhead_ms || 0}ms`,
      ),
    }),
  );

  const layersPanel = el(
    Panel,
    {
      React,
      title: t("节省构成", "Where the savings come from"),
      subtitle:
        headline.mcp_tokens_removed > 0
          ? t(`MCP 压缩 ${fmtInt(headline.mcp_compressions)} 次`, `${fmtInt(headline.mcp_compressions)} MCP compressions`)
          : "",
    },
    layers.map((layer) =>
      el(Bar, {
        React,
        key: layer.key,
        label: ZH ? layer.label_zh : layer.label_en,
        active: layer.active,
        share: layerTotal > 0 && layer.unit !== "usd" ? ((layer.amount || 0) / layerTotal) * 100 : 0,
        value:
          layer.unit === "usd"
            ? fmtUsd(layer.amount)
            : `${fmtCompact(layer.amount)}${layer.requests ? ` · ${fmtInt(layer.requests)}×` : ""}`,
        detail: ZH ? layer.detail : layer.detail_en,
      }),
    ),
    !layers.length ? el(React, "div", { className: "hr-empty" }, t("暂无数据", "No data")) : null,
  );

  const lifetimePanel = el(
    Panel,
    {
      React,
      title: t("累计节省", "Lifetime savings"),
      subtitle: lifetime.started_at ? t(`自 ${lifetime.started_at}`, `since ${lifetime.started_at}`) : "",
    },
    el(Row, {
      React,
      label: t("节省 Token", "Tokens saved"),
      value: fmtInt(lifetime.tokens_saved),
      mono: true,
    }),
    el(Row, {
      React,
      label: t("节省比例", "Savings"),
      value: fmtPct(lifetime.savings_percent),
      mono: true,
    }),
    el(Row, {
      React,
      label: t("节省金额", "Cost saved"),
      value: fmtUsd(lifetime.cost_saved_usd),
      mono: true,
    }),
    el(Row, {
      React,
      label: t("请求数", "Requests"),
      value: fmtInt(lifetime.requests),
      mono: true,
    }),
    el(Row, {
      React,
      label: t("输入 Token", "Input tokens"),
      value: fmtInt(lifetime.input_tokens),
      mono: true,
    }),
    lifetime.last_activity_at
      ? el(Row, {
          React,
          label: t("最近活动", "Last activity"),
          value: lifetime.last_activity_at,
          mono: true,
        })
      : null,
  );

  const historyPanel = el(
    Panel,
    {
      React,
      title: t("每次请求的节省", "Savings per request"),
      subtitle: `${(history.points || []).length} ${t("条", "points")}`,
    },
    el(Sparkline, { React, points: history.points || [] }),
    history.session && history.session.usd_saved
      ? el(Row, {
          React,
          label: t("本会话节省金额", "Session cost saved"),
          value: fmtUsd(history.session.usd_saved),
          mono: true,
        })
      : null,
  );

  const agentRows = agents.rows || [];
  const agentsPanel = el(
    Panel,
    {
      React,
      title: t("按 Agent 统计", "Per-agent savings"),
      subtitle: agents.totals && agents.totals.requests
        ? `${fmtInt(agents.totals.requests)} ${t("次请求", "requests")}`
        : "",
    },
    agentRows.length
      ? el(
          React,
          "table",
          { className: "hr-table" },
          el(
            React,
            "thead",
            null,
            el(
              React,
              "tr",
              null,
              el(React, "th", null, t("Agent", "Agent")),
              el(React, "th", null, t("请求", "Req")),
              el(React, "th", null, t("节省", "Saved")),
              el(React, "th", null, t("占比", "Share")),
            ),
          ),
          el(
            React,
            "tbody",
            null,
            agentRows.map((row) =>
              el(
                React,
                "tr",
                { key: row.agent },
                el(React, "td", { className: "hr-td-name", title: row.agent }, row.label || row.agent),
                el(React, "td", { className: "hr-num" }, fmtInt(row.requests)),
                el(React, "td", { className: "hr-num" }, fmtCompact(row.tokens_saved)),
                el(React, "td", { className: "hr-num" }, fmtPct(row.share_of_saved_percent)),
              ),
            ),
          ),
        )
      : el(React, "div", { className: "hr-empty" }, t("暂无 Agent 维度数据", "No per-agent data yet")),
  );

  const routed = Boolean(routing.routed);
  const routingPanel = el(
    Panel,
    {
      React,
      title: t("路由（Routing）", "Routing"),
      subtitle: routed
        ? t("流量已走 Headroom", "Traffic goes through Headroom")
        : t("流量直连模型供应商", "Traffic goes straight to the provider"),
      extra: el(
        React,
        "span",
        { className: `hr-badge ${routed ? "hr-badge-ok" : "hr-badge-idle"}` },
        routed ? t("已启用", "Enabled") : t("未启用", "Disabled"),
      ),
    },
    el(Row, {
      React,
      label: t("当前默认模型", "Active model"),
      value:
        (routing.active && `${routing.active.provider_id || "—"} / ${routing.active.model || "—"}`) || "—",
      mono: true,
    }),
    el(Row, {
      React,
      label: t("Provider base_url", "Provider base_url"),
      value: (routing.provider && routing.provider.base_url) || t("（尚未创建）", "(not created yet)"),
      mono: true,
    }),
    el(Row, {
      React,
      label: t("插件期望地址", "Expected base_url"),
      value: routing.expected_base_url || "—",
      mono: true,
    }),
    routing.drift && routing.drift.length
      ? el(
          React,
          "p",
          { className: "hr-note hr-note-warn" },
          t("检测到配置漂移：", "Drift detected: "),
          routing.drift.join("；"),
        )
      : null,
    el(
      React,
      "div",
      { className: "hr-actions" },
      el(
        React,
        "button",
        {
          type: "button",
          className: "hr-btn hr-btn-primary",
          disabled: Boolean(busy),
          onClick: () => run(t("启用路由", "Enable routing"), "/routing/apply", "POST"),
        },
        busy ? t("处理中…", "Working…") : t("启用路由", "Enable routing"),
      ),
      el(
        React,
        "button",
        {
          type: "button",
          className: "hr-btn hr-btn-ghost",
          disabled: Boolean(busy),
          onClick: () => run(t("关闭路由", "Disable routing"), "/routing/disable", "POST"),
        },
        t("关闭路由", "Disable routing"),
      ),
    ),
    note ? el(React, "p", { className: `hr-note hr-note-${note.kind}` }, note.text) : null,
  );

  const configFile = config.config_file || {};
  const sources = config.sources || {};
  const configPanel = el(
    Panel,
    {
      React,
      title: t("配置", "Configuration"),
      subtitle: configFile.found
        ? t("来自插件配置文件", "from the plugin config file")
        : t("使用内置默认值", "using built-in defaults"),
      extra: el(
        React,
        "button",
        {
          type: "button",
          className: "hr-btn hr-btn-small",
          disabled: Boolean(busy),
          onClick: () => run(t("重载配置", "Reload config"), "/config/reload", "POST"),
        },
        t("重载配置", "Reload config"),
      ),
    },
    el(Row, {
      React,
      label: t("Headroom 地址", "Headroom URL"),
      value: `${config.base_url || "—"}  (${sources["headroom.base_url"] || "default"})`,
      mono: true,
    }),
    el(Row, {
      React,
      label: t("配置文件", "Config file"),
      value: configFile.path || "—",
      mono: true,
    }),
    el(Row, {
      React,
      label: t("Provider", "Provider"),
      value: `${(config.routing && config.routing.provider_id) || "headroom"} · ${
        (config.routing && config.routing.chat_model) || "OpenAIChatModel"
      }`,
      mono: true,
    }),
    el(Row, {
      React,
      label: t("超时", "Timeout"),
      value: `${config.timeout_seconds || 5}s`,
      mono: true,
    }),
    config.routing && config.routing.api_key_set
      ? el(Row, {
          React,
          label: t("转发用 API Key", "Forwarded API key"),
          value: config.routing.api_key_masked || t("已配置", "set"),
          mono: true,
        })
      : null,
    configFile.error
      ? el(React, "p", { className: "hr-note hr-note-error" }, configFile.error)
      : null,
    (config.warnings || []).length
      ? el(
          React,
          "ul",
          { className: "hr-warnings" },
          (config.warnings || []).map((warning, index) =>
            el(React, "li", { key: index }, warning),
          ),
        )
      : null,
    error ? el(React, "p", { className: "hr-note hr-note-error" }, error.message) : null,
  );

  return el(
    React,
    "div",
    { className: "hr-root" },
    header,
    kpis,
    el(React, "div", { className: "hr-grid" }, layersPanel, lifetimePanel),
    historyPanel,
    el(React, "div", { className: "hr-grid" }, agentsPanel, routingPanel),
    configPanel,
    el(
      React,
      "footer",
      { className: "hr-footer" },
      t(
        "数据来自 Headroom 的 /stats 与 /stats-history，页面只读，不会修改 Headroom 配置。",
        "Numbers come from Headroom's /stats and /stats-history; this page is read-only.",
      ),
    ),
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const STYLE_ID = "hr-styles";

const CSS = `
.hr-root {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 20px 24px 40px;
  max-width: 1180px;
  margin: 0 auto;
  color: var(--qp-text-primary, var(--ant-color-text, #1f2329));
  font-size: 13px;
}
.hr-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.hr-title { margin: 0 0 6px; font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }
.hr-subtitle { margin: 0; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; color: var(--qp-text-secondary, var(--ant-color-text-secondary, #646a73)); font-size: 12px; }
.hr-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.hr-updated { font-size: 12px; color: var(--qp-text-secondary, #8a9099); }
.hr-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.hr-badge { display: inline-flex; align-items: center; gap: 5px; padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; letter-spacing: .02em; }
.hr-badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.hr-badge-ok { color: #0f9d58; background: rgba(15, 157, 88, 0.12); }
.hr-badge-off { color: #d93025; background: rgba(217, 48, 37, 0.12); }
.hr-badge-idle { color: #8a9099; background: rgba(138, 144, 153, 0.14); }
.hr-btn {
  border: 1px solid var(--qp-border, rgba(0,0,0,.14));
  background: var(--qp-surface, #fff);
  color: inherit; border-radius: 8px; padding: 6px 12px; font-size: 12px;
  cursor: pointer; transition: background .15s ease, border-color .15s ease, opacity .15s ease;
}
.hr-btn:hover:not(:disabled) { border-color: var(--qp-accent, #1677ff); color: var(--qp-accent, #1677ff); }
.hr-btn:disabled { opacity: .55; cursor: default; }
.hr-btn-primary { background: var(--qp-accent, #1677ff); border-color: var(--qp-accent, #1677ff); color: #fff; }
.hr-btn-primary:hover:not(:disabled) { color: #fff; filter: brightness(1.06); }
.hr-btn-ghost { background: transparent; }
.hr-btn-small { padding: 3px 9px; font-size: 11px; }
.hr-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.hr-kpi {
  border: 1px solid var(--qp-border, rgba(0,0,0,.08));
  border-radius: 12px; padding: 14px 16px; background: var(--qp-surface, #fff);
  display: flex; flex-direction: column; gap: 4px;
}
.hr-kpi-accent { border-color: rgba(22,119,255,.35); box-shadow: 0 1px 2px rgba(22,119,255,.08); }
.hr-kpi-label { font-size: 12px; color: var(--qp-text-secondary, #646a73); }
.hr-kpi-value { font-size: 24px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.15; font-variant-numeric: tabular-nums; }
.hr-kpi-unit { font-size: 12px; font-weight: 500; margin-left: 4px; color: var(--qp-text-secondary, #646a73); }
.hr-kpi-hint { font-size: 11px; color: var(--qp-text-secondary, #8a9099); }
.hr-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; align-items: start; }
.hr-panel { border: 1px solid var(--qp-border, rgba(0,0,0,.08)); border-radius: 12px; background: var(--qp-surface, #fff); overflow: hidden; }
.hr-panel-head { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--qp-border, rgba(0,0,0,.06)); }
.hr-panel-title { margin: 0; font-size: 13px; font-weight: 600; }
.hr-panel-sub { font-size: 11px; color: var(--qp-text-secondary, #8a9099); }
.hr-panel-head > :last-child { margin-left: auto; }
.hr-panel-body { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.hr-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.hr-row-key { color: var(--qp-text-secondary, #646a73); font-size: 12px; white-space: nowrap; }
.hr-row-value { text-align: right; word-break: break-all; }
.hr-bar-row { display: flex; flex-direction: column; gap: 5px; }
.hr-bar-row-idle { opacity: .55; }
.hr-bar-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.hr-bar-label { font-size: 12px; font-weight: 550; }
.hr-bar-value { font-size: 12px; font-variant-numeric: tabular-nums; color: var(--qp-text-secondary, #646a73); }
.hr-bar-track { height: 6px; border-radius: 999px; background: rgba(127,127,127,.14); overflow: hidden; }
.hr-bar-fill { height: 100%; border-radius: 999px; background: linear-gradient(90deg, #1677ff, #4f9dff); transition: width .3s ease; }
.hr-bar-detail { font-size: 11px; color: var(--qp-text-secondary, #8a9099); }
.hr-spark { color: #1677ff; display: flex; flex-direction: column; gap: 6px; }
.hr-spark-svg { width: 100%; height: 96px; display: block; }
.hr-spark-scale { display: flex; justify-content: space-between; font-size: 11px; color: var(--qp-text-secondary, #8a9099); }
.hr-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.hr-table th { text-align: left; font-weight: 550; color: var(--qp-text-secondary, #8a9099); padding: 4px 6px 8px; border-bottom: 1px solid var(--qp-border, rgba(0,0,0,.06)); }
.hr-table td { padding: 6px; border-bottom: 1px solid var(--qp-border, rgba(0,0,0,.04)); }
.hr-table tr:last-child td { border-bottom: none; }
.hr-td-name { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hr-num { text-align: right; font-variant-numeric: tabular-nums; }
.hr-empty { font-size: 12px; color: var(--qp-text-secondary, #8a9099); padding: 8px 0; }
.hr-alert { border-color: rgba(217,48,37,.35); }
.hr-alert-title { margin: 0 0 6px; font-size: 15px; }
.hr-alert-text { margin: 0; font-size: 12px; }
.hr-muted { color: var(--qp-text-secondary, #646a73); }
.hr-code { margin: 0; padding: 10px 12px; border-radius: 8px; background: rgba(127,127,127,.1); font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 11.5px; white-space: pre-wrap; }
.hr-note { margin: 0; font-size: 12px; padding: 7px 10px; border-radius: 8px; }
.hr-note-ok { background: rgba(15,157,88,.1); color: #0f9d58; }
.hr-note-error { background: rgba(217,48,37,.1); color: #d93025; }
.hr-note-warn { background: rgba(230,145,0,.12); color: #b26a00; }
.hr-warnings { margin: 0; padding-left: 18px; font-size: 11.5px; color: #b26a00; display: flex; flex-direction: column; gap: 2px; }
.hr-skeleton { padding: 28px; text-align: center; color: var(--qp-text-secondary, #8a9099); font-size: 12px; }
.hr-footer { font-size: 11px; color: var(--qp-text-secondary, #8a9099); }
@media (max-width: 640px) {
  .hr-root { padding: 14px 12px 28px; }
  .hr-kpi-value { font-size: 20px; }
}
`;

function injectStyles(doc) {
  if (!doc || doc.getElementById(STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------

function install(paw) {
  const host = paw.host || {};
  hostRef = host;
  const React = host.React;
  if (!React) return;
  injectStyles(typeof document !== "undefined" ? document : null);

  const component = function HeadroomRouteComponent() {
    return el(HeadroomPage, { React });
  };

  try {
    paw.route.add(PLUGIN_ID, {
      id: ROUTE_ID,
      path: ROUTE_PATH,
      component,
      meta: {
        domain: "extensions",
        title: { "zh-CN": "Headroom 节省", "en-US": "Headroom Savings" },
        description: {
          "zh-CN": "查看 Headroom 代理压缩上下文节省的 token 与费用",
          "en-US": "Token and cost savings produced by the Headroom proxy",
        },
        scope: "global",
        layout: "standard",
        navigation: "rail",
      },
    });
  } catch (err) {
    console.warn("[headroom] route registration failed", err);
  }

  try {
    paw.menu.add(PLUGIN_ID, {
      id: MENU_ID,
      location: "primary.settings",
      order: 62,
      icon: "🗜️",
      label: () => t("Headroom 节省", "Headroom Savings"),
      route: ROUTE_ID,
    });
  } catch (err) {
    console.warn("[headroom] menu registration failed", err);
  }
}

function whenHost(deadlineMs) {
  const limit = deadlineMs || 20000;
  const started = Date.now();
  const tick = () => {
    const paw = typeof window !== "undefined" ? window.QwenPaw : null;
    if (paw && paw.host && paw.route && paw.menu && paw.host.React) {
      install(paw);
      return;
    }
    if (Date.now() - started > limit) {
      console.warn("[headroom] QwenPaw console host not found; page not registered");
      return;
    }
    setTimeout(tick, 50);
  };
  tick();
}

whenHost();
