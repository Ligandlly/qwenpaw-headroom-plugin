# Headroom 上下文压缩插件（QwenPaw）

把 QwenPaw 的模型请求接到 [Headroom](https://github.com/headroom-ai) 压缩代理上，并在控制台里加一个
「Headroom 节省」页面，实时展示代理替你省下了多少 token / 多少钱、命中了哪些压缩层、每个 agent 的贡献，
以及当前流量到底走没走代理。

后端零第三方依赖（纯标准库 + FastAPI 由 QwenPaw 自带），前端是一个**不需要构建**的 ES module，
所以整个插件可以直接打成一个 zip 拷进内网机器安装。

---

## 1. 功能一览

| 能力 | 说明 |
| --- | --- |
| 流量接管 | 自动创建/复用 QwenPaw 自定义 provider，把默认 chat 模型指向 Headroom 的 `/v1` |
| 一键开关 | 页面里「启用路由 / 关闭路由」，关闭时恢复原来的 provider + 模型 |
| 节省看板 | 省下的 token、省下的美元、压缩率、四个压缩层的明细、逐请求折线、按 agent 排行 |
| 代理健康 | 版本、模式、uptime、探测延迟；代理没起来时给出中文排障提示和启动命令 |
| 离线分发 | `headroom.config.json` 里硬编码代理地址，安装后无需联网、无需任何依赖安装 |
| 配置热重载 | 改完 `headroom.config.json` 点「重载配置」即可生效，不用重启 QwenPaw |

---

## 2. 目录结构

```
plugin.json                     # 插件清单（type=general，不带 sdk 键 → 拿到旧版 PluginApi 的路由能力）
plugin.py                       # 后端入口：注册 /api/headroom 路由 + 启动钩子
headroom.config.json            # ★ 离线分发用的配置文件（硬编码 headroom 地址）
headroom_plugin/
  config.py                     # 分层配置合并（默认值 → 本文件 → QwenPaw 配置 → 环境变量）
  client.py                     # 访问 headroom 的 stdlib HTTP 客户端（带超时/错误分类）
  savings.py                    # 把 headroom 原始 JSON 归一化成页面要的视图模型
  routing.py                    # 创建/切换 provider，改默认模型，记录原始状态以便还原
  runtime.py                    # 进程内单例：配置缓存、客户端缓存、快照缓存、路由锁
  routes.py                     # FastAPI 路由（挂到 /api/headroom）
frontend/dist/index.js          # 页面（console 插件，纯 ESM，无构建）
scripts/package_offline.py      # 打离线 zip（可复现构建 + sha256）
scripts/smoke_routes.py         # 不依赖 pytest 的端到端冒烟脚本
tests/                          # pytest 单元测试 + 前后端字段契约测试
```

---

## 3. 安装

### 3.1 在线（本机已装 QwenPaw）

```bash
git clone <this-repo> ~/projects/qwenpaw-headroom-plugin
mkdir -p ~/.qwenpaw/plugins
cp -r ~/projects/qwenpaw-headroom-plugin ~/.qwenpaw/plugins/headroom
# 重启 QwenPaw 或在控制台「插件」页重新加载
```

### 3.2 离线（内网机器）

```bash
# 在有网的机器上
python3 scripts/package_offline.py --out-dir release
# 产物：release/headroom-plugin-1.0.0.zip + .sha256

# 拷到内网机器后（先改配置里的地址，见第 4 节）
unzip headroom-plugin-1.0.0.zip -d ~/.qwenpaw/plugins/
```

zip 里的顶层目录就是 `headroom/`，解压后即 `~/.qwenpaw/plugins/headroom/`。

安装完成后：

```bash
curl -s http://127.0.0.1:8088/api/headroom/health -H "Authorization: Bearer <token>" | python3 -m json.tool
```

---

## 4. 配置：`headroom.config.json`（核心）

这个文件是**离线分发的唯一开关**：把 `headroom.base_url` 改成内网代理地址，其他一律不用动。
文件支持 `//` 和 `/* */` 注释（JSONC），所以可以在里面写清楚每个字段的用途。

```jsonc
{
  "headroom": {
    "base_url": "http://127.0.0.1:8787",   // ★ 离线分发时改这里，可写 host:port
    "timeout_seconds": 5.0,
    "use_env_proxy": false                  // 内网有 HTTP_PROXY 时保持 false
  },
  "routing": {
    "enabled": true,                        // 插件加载时是否自动接管流量
    "provider_id": "headroom",              // 自定义 provider 的 id
    "provider_name": "Headroom 压缩代理",
    "api_path": "/v1",                      // 拼在 base_url 后面
    "chat_model": "OpenAIChatModel",
    "model": "",                            // 留空 = 采纳当前默认模型
    "adopt_active_model": true,
    "api_key": "",                          // 留空 = 复用当前 provider 的 key（推荐）
    "restore_on_disable": true,
    "state_file": ""                        // 留空 = ~/.qwenpaw/plugin-data/headroom/routing-state.json
  },
  "ui": {
    "refresh_seconds": 20,                  // 页面自动刷新间隔
    "history_limit": 60,                    // 折线图取最近多少个请求
    "dashboard_path": "/dashboard"          // headroom 自带看板的路径
  }
}
```

### 4.1 优先级（后者覆盖前者）

1. 代码里的默认值（`headroom_plugin/config.py` 的 `DEFAULTS`）
2. **本文件 `headroom.config.json`**
3. QwenPaw 用户配置 `~/.qwenpaw/config.json` 的 `plugins.headroom` 段（可用控制台 UI 覆盖）
4. 环境变量

页面和 `GET /api/headroom/config` 都会给出每个字段的最终来源（`plugin-file` / `qwenpaw-config` / `env` / `default`），
排查「我明明改了怎么没生效」时先看这里。

### 4.2 环境变量

| 变量 | 作用 |
| --- | --- |
| `HEADROOM_BASE_URL` / `QWENPAW_HEADROOM_URL` | 覆盖代理地址 |
| `HEADROOM_TIMEOUT_SECONDS` | 覆盖超时 |
| `HEADROOM_ROUTING_ENABLED` | `1/0` 覆盖是否自动接管 |
| `HEADROOM_HISTORY_LIMIT` | 覆盖折线图点数 |

`base_url` 允许只写 `host:port`（自动补 `http://`），结尾斜杠会被去掉；非 http(s) 会直接报错而不是静默失败。

---

## 5. 路由是怎么做的

Headroom 是**透传型**代理：它只改写请求体的上下文，然后把请求转发给真正的上游，并且会把调用方的
`Authorization` 原样带上。因此插件不去碰 API key，只做三件事：

1. 新建（或复用）一个 QwenPaw 自定义 provider `headroom`，`base_url = <base_url><api_path>`；
2. 把默认 chat 模型切到这个 provider 上的目标模型（`routing.model`，留空则采纳当前默认模型）；
3. 把切换前的 provider/模型写进 `routing-state.json`。

「关闭路由」时按 state 文件还原默认模型；只有当 provider 是本插件新建的（`is_custom`）才会顺手删掉它，
不会误删用户自己配置的 provider。

> 因为 Headroom 转发时会保留调用方凭据，插件不需要、也不建议在配置里写明文 key。
> 若你的部署必须由插件注入 key，填 `routing.api_key` 即可（页面只会看到掩码）。

### 5.1 页面上的「路由状态」

| 字段 | 含义 |
| --- | --- |
| 已接管 routed | 当前默认 chat 模型是否指向 `headroom` provider |
| 期望地址 | 配置期望的 `base_url + api_path` |
| 漂移 drift | provider 的实际 `base_url` 与期望不一致（有人手改过控制台）时列出来 |
| 可还原 | `routing-state.json` 里是否还留着切换前的模型 |

---

## 6. 节省看板（页面）

入口：控制台 → 设置 → **Headroom 节省**（左侧导航也可直达，路由 `/headroom`）。

展示内容：

- **顶部指标**：已省 token、已省钱（USD）、被压缩的请求数、总请求数 / 探测延迟
- **压缩分层**：`上下文压缩`、`前缀缓存`、`输出精简`、`工具搜索` 四层，各自的 token/美元与是否生效
- **本次会话 vs 累计**：`/stats-history` 的 session 与 lifetime
- **逐请求折线**：每个请求省下多少 token（SVG，无依赖）
- **按 agent 排行**：谁消耗最多、谁省得最多、占比
- **路由面板**：状态 + 「启用路由 / 关闭路由」按钮
- **配置面板**：当前生效配置、字段来源、警告、「重载配置」按钮
- **代理离线态**：中文提示 + 建议执行的命令 + 配置文件路径

页面只和 `/api/headroom/*` 通信，浏览器永远不直连代理（省掉 CORS，也让代理地址只存在于服务端配置里）。

---

## 7. HTTP API

全部挂在 `/api/headroom` 下（需要 QwenPaw 的 Bearer token）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 插件身份 + 代理探测 + 路由状态 |
| GET | `/config` | 生效配置（key 已掩码）、字段来源、警告 |
| POST | `/config/reload` | 重新读取 `headroom.config.json` |
| GET | `/savings?history_limit=60&force=true` | 看板数据（页面用的就是它） |
| GET | `/stats` | 代理的原始 `/stats` `/stats-lifetime` `/stats-history` 合并结果 |
| POST | `/probe` | 单次探测代理 |
| GET | `/routing` | 路由状态（不含大 payload） |
| POST | `/routing/apply` | 接管流量（无模型可采纳时返回 409） |
| POST | `/routing/disable` | 还原 |
| GET | `/settings/preview` | 只计算「将会配置成什么」，不做修改 |

错误统一返回 `{"ok": false, "kind": "...", "message": "..."}`，`kind ∈ {unreachable, timeout, http, bad-json, bad-payload, host-unavailable, no-model}`。

---

## 8. 验证与测试

```bash
# 单元测试 + 前后端字段契约（不需要 headroom 在跑，全部用桩服务器）
python3 -m pytest tests -q

# 端到端冒烟：需要 fastapi/httpx，用 QwenPaw 自带的 venv 跑
/home/lly/.local/share/uv/tools/qwenpaw/bin/python scripts/smoke_routes.py
```

- `tests/test_config.py` 配置分层、注释解析、URL 归一化
- `tests/test_client.py` 超时/坏 JSON/HTTP 错误分类，部分接口失败时仍能出快照
- `tests/test_savings.py` 视图模型归一化（含代理离线时的降级形态）
- `tests/test_routing.py` 创建/复用/还原 provider，漂移检测（不需要装 QwenPaw）
- `tests/test_runtime.py` 配置热重载、客户端/快照缓存、启动钩子
- `tests/test_routes.py` 每个端点（无 fastapi 时自动 skip）
- `tests/test_frontend_contract.py` 反向校验：页面读的每个字段后端都必须真的发出来

---

## 9. 排障

| 现象 | 处理 |
| --- | --- |
| 页面显示「连不上 Headroom」 | 确认代理在跑：`systemctl --user status headroom-proxy`；或 `headroom proxy --host 127.0.0.1 --port 8787` |
| 读数一直是 0 | 流量还没走代理：看「路由状态」是否「已接管」，或点「启用路由」 |
| 启用路由返回 409 `no-model` | 先在控制台选一个默认模型，或在配置里填 `routing.model` |
| 改了地址没生效 | 看配置面板的「来源」；点「重载配置」，或检查环境变量是否覆盖 |
| `drift` 非空 | 有人手改过 provider 的 base_url；再点一次「启用路由」即可纠正 |
| `host_available: false` | 插件没拿到 QwenPaw 的 provider 子系统（版本不匹配）；看板仍可用，只是不能接管流量 |

---

## 10. License

MIT，见 `LICENSE`。
