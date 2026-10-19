# memory — 本地记忆 MCP

纯本地的长期记忆服务（MCP stdio），供 AI 助手存取事实、偏好、决策与项目状态。
**数据不出本机**：SQLite + [sqlite-vec](https://github.com/asg017/sqlite-vec) 向量检索 + 本地 ONNX 嵌入模型。

## 架构

```
MCP 客户端 (ZCode / pi / pi-web-ui / Cursor ...)
        │ streamable-http  http://127.0.0.1:8766/mcp
        ▼
pm2 守护进程 "memory-mcp"（ecosystem.config.js，崩溃自动重启、开机自启）
        │
server.py ── sqlite-vec (KNN 向量检索) ── memory.db
        │
        ▼
embed.py ── 嵌入提供者（自动选择）
   ├─ onnx-bge-small-zh-v1.5（默认，512 维，中英文语义，models/ 下约 95MB）
   └─ hash（兜底，字符 n-gram 哈希，零依赖；模型缺失/损坏时自动降级）
```

- 检索用 bge 查询指令前缀，中文同义改写也能召回（实测无共同关键词仍命中）。
- 嵌入提供者/维度变化时启动自动重建向量索引（也可手动 `server.py reindex`）。
- 英文查询中文记忆是弱项（bge-small-zh 特性）；中文场景为主则无影响。

## 工具（7 个）

| 工具 | 说明 |
|---|---|
| `memory_write(content, tags?, kind?, importance?, scope?)` | 存一条记忆，返回 12 位短 id；长内容自动滑动窗口切块 |
| `memory_search(query, k=8, tag?)` | 语义检索；内部扩大候选池后按相关性、多样性和重要性重排 |
| `memory_list(limit=20, tag?, q?)` | 按时间倒序列表，tag 精确过滤 / q 子串过滤 |
| `memory_get(id)` | 按 id 或唯一前缀取一条 |
| `memory_update(id, content?, tags?, kind?, importance?, scope?)` | 更新，content 变化自动重算向量 |
| `memory_delete(id)` | 按 id 或唯一前缀删除 |
| `memory_stats()` | 条数、时间范围、库大小、嵌入模型 |

## 注册（HTTP 常驻服务）

ZCode 用户级配置 `~/.zcode/cli/config.json` → `mcp.servers`：

```json
"memory": { "type": "http", "url": "http://127.0.0.1:8766/mcp" }
```

pi agent 全局配置 `~/.pi/agent/mcp.json`：

```bash
pi mcp add memory --url http://127.0.0.1:8766/mcp --description "..."
pi mcp list   # 验证
```

服务现监听 `0.0.0.0:8766`（局域网 `192.168.5.11:8766/mcp` 与 Tailscale `100.78.31.75:8766/mcp` 均可直连；本机客户端仍用 `127.0.0.1`）。注意 `/mcp` 是 POST 型 JSON-RPC 端点，浏览器直接打开只会看到 400 错误 JSON，属正常现象；测试请用 MCP 客户端或 `curl -X POST`。**该端点无鉴权，请勿在路由器上做端口映射暴露到公网。**

## 服务管理（pm2）

```bash
pm2 start /root/memory/ecosystem.config.js   # 首次启动（已完成）
pm2 restart memory-mcp                       # 重启（改代码后）
pm2 logs memory-mcp                          # 看日志
pm2 monit                                    # 资源监控
pm2 save                                     # 固化进程列表（开机自启由 pm2-root.service 提供）
```

## 使用与维护

```bash
cd /root/memory
./.venv/bin/python test_memory.py        # 全套测试（直连 + MCP 端到端）
./.venv/bin/python server.py reindex     # 用当前嵌入模型重建向量索引
sqlite3 memory.db                        # 直接查库（memories 表 / memories_vec 向量表）
```

备份就是复制 `memory.db`。清空 = 删除 `memory.db`（下次启动自动重建）。

### 检索与滑动窗口切块

记忆带有 `kind`、`importance`、`scope` 元数据。支持的 kind：`rule`、`preference`、`decision`、`lesson`、`status`、`fact`、`report`。检索会先取更大的候选池，再用重要性和 MMR 风格多样性重排，避免同类记忆占满 Top-K。

长记忆不会按条数删除。超过 `MEMORY_CHUNK_THRESHOLD` 的内容会按 `MEMORY_CHUNK_SIZE` 字符切成带重叠的短上下文块，分别建立向量索引；检索命中 chunk 后仍关联父记忆，并用父记忆 id 做去重惩罚。默认窗口 1200 字符、重叠 200 字符。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEMORY_DB` | `/root/memory/memory.db` | 数据库路径 |
| `MEMORY_HTTP_HOST` | `127.0.0.1` | serve 模式监听地址 |
| `MEMORY_HTTP_PORT` | `8766` | serve 模式监听端口 |
| `MEMORY_EMBED_PROVIDER` | `auto` | `auto` / `onnx` / `hash` |
| `MEMORY_MODEL_DIR` | `models/bge-small-zh-v1.5` | ONNX 模型目录 |
| `MEMORY_ONNX_THREADS` | `4` | ONNX 推理线程数 |
| `MEMORY_CHUNK_SIZE` | `1200` | 长记忆滑动窗口大小（字符） |
| `MEMORY_CHUNK_OVERLAP` | `200` | 相邻窗口重叠大小（字符） |
| `MEMORY_CHUNK_THRESHOLD` | `1201` | 超过此长度才切块 |
| `MEMORY_SEARCH_CANDIDATE_MULTIPLIER` | `10` | 检索内部候选数相对 k 的倍数 |
| `MEMORY_SEARCH_CANDIDATE_MAX` | `500` | 检索内部候选数上限 |
| `HF_ENDPOINT` | `https://huggingface.co` | 仅下载模型时使用（内置 hf-mirror.com 兜底） |

重装模型：`./.venv/bin/python download_model.py`

## 依赖

独立 venv（`.venv`，复用系统 numpy/onnxruntime）：`mcp`、`sqlite-vec`、`tokenizers`。
