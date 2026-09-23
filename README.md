# mcp-memory-mail

部署在 Cloudflare Workers（免费档够用）的个人远程 MCP 服务器，提供两组能力：

- **mail_send** — 通过 QQ 邮箱 SMTP（smtp.qq.com:465 SSL）把文本/Markdown（含附件）发出，授权码存 Worker secret，不经客户端
- **memory_write / memory_update / memory_search / memory_list / memory_delete / archive_save** — 跨机器、跨 AI 工具的共享记忆体（D1 + FTS5 关键词检索 + Vectorize 语义检索，Workers AI 生成 embedding）

所有机器上的 ZCode / Cursor / Claude Code 等都连这一个端点，共享同一份记忆。

## 已验证（本地 wrangler dev 冒烟测试通过）

- MCP initialize / tools/call 全链路
- memory_write → D1 写入 → FTS5 检索命中（Vectorize/AI 仅线上可用，本地自动降级）
- mail_send MIME 构造正常，收件人白名单校验生效
- UTF-8 中文存储无损

## 部署步骤

```bash
npm install

# 1. 登录 Cloudflare
npx wrangler login

# 2. 创建 D1，把输出的 database_id 填进 wrangler.jsonc
npx wrangler d1 create memory
# 3. 建表（本地 + 远程各一次）
npx wrangler d1 execute memory --local  --file=./schema.sql
npx wrangler d1 execute memory --remote --file=./schema.sql

# 4. 创建 Vectorize 索引（bge-m3 是 1024 维）
npx wrangler vectorize create memory --dimensions=1024 --metric=cosine

# 5. 设置密钥：MCP 访问令牌（自己生成一个随机串，如 openssl rand -hex 32）
npx wrangler secret put MCP_TOKEN
# 6. 发件人地址（必须是 Cloudflare 验证过域名下的地址；也可以直接改 wrangler.jsonc 的 vars.MAIL_FROM）
npx wrangler secret put MAIL_FROM

# 7. 部署
npm run deploy
```

### 邮件收发前提（一次性）

1. **发件域名**：MAIL_FROM 用的域名必须托管在 Cloudflare 并开启 Email Routing。
2. **收件人**：编辑 `wrangler.jsonc` 里 `allowed_destination_addresses`，填你自己的邮箱，
   然后到 Cloudflare 控制台 → Email Routing → Destination addresses 完成该邮箱的验证。
   发到「已验证地址」永久免费，不计日配额。
3. 部署后先发一封测试邮件，Cloudflare 对新账号有保守日配额，个人自用足够。

### 认证模型

v1 用静态 Bearer Token（`MCP_TOKEN` secret）：所有客户端共用一个令牌，走 HTTPS，个人使用足够。
要升级到 per-client OAuth（`workers-oauth-provider` + Cloudflare Access）时，只需替换 `src/index.js`
里的 `authorized()` 与入口包装，工具代码不变。

## 各机器接入

### ZCode（`~/.zcode/cli/config.json` 或工作区 `.zcode/config.json`）

```json
{
  "mcp": {
    "servers": {
      "memory-mail": {
        "type": "http",
        "url": "https://mcp-memory-mail.<你的子域>.workers.dev/mcp",
        "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add --transport http memory-mail https://mcp-memory-mail.<子域>.workers.dev/mcp \
  --header "Authorization: Bearer <MCP_TOKEN>"
```

### Cursor（`~/.cursor/mcp.json`）

```json
{
  "mcpServers": {
    "memory-mail": {
      "url": "https://mcp-memory-mail.<子域>.workers.dev/mcp",
      "headers": { "Authorization": "Bearer <MCP_TOKEN>" }
    }
  }
}
```

不支持自定义 header 的老客户端，用 `npx mcp-remote https://.../mcp --header "Authorization: Bearer <MCP_TOKEN>"` 桥接。

## 工具说明

| 工具 | 用途 |
|---|---|
| `memory_write` | 写一条记忆。自包含、一事一条；重复内容自动去重 |
| `memory_update` | 按 id 部分更新已有记忆（支持短 id 前缀）：只改传入的字段（content/tags/machine/tool），其余保持不变，返回更新后的完整条目 |
| `memory_search` | 语义检索（Vectorize）+ 关键词兜底（FTS5）。任务开始时先搜一遍 |
| `memory_list` | 最近条目，可按 machine / tag 过滤 |
| `memory_delete` | 按 id（支持前缀）删除，同步清 Vectorize |
| `memory_reindex` | 运维：全部记忆重新向量化（换 embedding 模型后跑一次），不改 `updated_at` |
| `mail_send` | 发邮件到白名单邮箱；`attachment` + `filename` 可带附件（如完整会话 Markdown） |
| `archive_save` | 大段 Markdown 存档进 D1（会话留档） |

### 记忆写入约定（给所有 Agent 的提示词约定）

- 内容自包含：不写「上面那个 bug」，写「blog 项目里 hexo 渲染中文乱码，原因是...」
- 带 `machine` 和 `tool` 溯源；更新已有条目用 `memory_update`（短 id 前缀即可，如检索返回的 8 位 id），不要重复新增
- 敏感信息（密钥、密码）不进记忆库

## 免费额度对照

| 资源 | 免费额度 | 本项目用法 |
|---|---|---|
| Workers | 10 万请求/天 | 绰绰有余 |
| D1 | 512MB/库，5GB/账号 | 文本记忆，几年用不完 |
| Vectorize | 免费 100 索引 | 用 1 个 |
| Workers AI | 10,000 Neurons/天 | bge-m3 embedding |
| 邮件 | 发已验证地址永久免费 | 全部发给自己 |

## 故障排查

- `wrangler tail` 看线上日志
- `GET /health` 检查绑定：`{"ok":true,"mail":true,"ai":true,"db":true,"vec":true}`
- 401 → 客户端没带或带错 `Authorization` 头
- 邮件报 `E_RECIPIENT_NOT_ALLOWED` → 收件人不在 `allowed_destination_addresses` 或未验证
