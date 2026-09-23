/**
 * Personal remote MCP server on Cloudflare Workers (free plan).
 *
 * Tools:
 *   mail_send       — send text/markdown via QQ SMTP (smtp.qq.com:465 SSL)
 *   memory_write    — upsert a memory entry (D1 + FTS5 + Vectorize embedding)
 *   memory_update   — partial update of an existing memory by id (short prefix ok)
 *   memory_search   — semantic (Vectorize) + keyword (FTS5) search
 *   memory_list     — recent entries / filter by machine or tag
 *   memory_delete   — delete by id (short prefix ok)
 *   memory_reindex  — admin: re-embed all memories into Vectorize
 *   archive_save    — store a big session markdown in D1
 *
 * Auth: Bearer token (env.MCP_TOKEN secret). Clients send
 *   Authorization: Bearer <token>
 * on the Streamable HTTP endpoint /mcp.
 */
import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { connect } from "cloudflare:sockets";
import { createMimeMessage } from "mimetext";
import { z } from "zod";

const EMBED_MODEL = "@cf/baai/bge-m3"; // 1024 dims（注意：没有 bge-m3-base 这个型号）

function json(message, text) {
  return { content: [{ type: "text", text: text ?? JSON.stringify(message, null, 2) }] };
}

function textOut(text) {
  return { content: [{ type: "text", text }] };
}

function errOut(err) {
  return { content: [{ type: "text", text: `Error: ${err?.message || String(err)}` }], isError: true };
}

function id() {
  return crypto.randomUUID();
}

/* ---------------------------------- mail ---------------------------------- */
/*
 * Direct SMTP via cloudflare:sockets — QQ 邮箱 smtp.qq.com:465（隐式 TLS）。
 * 授权码存 Worker secret: SMTP_USER（邮箱地址）、SMTP_PASS（授权码）。
 * Workers 的 100k 日请求额度对个人发信完全够用。
 */

const SMTP_HOST = "smtp.qq.com";
const SMTP_PORT = 465;

function buildMailMime({ from, to, subject, body, filename, attachment }) {
  const msg = createMimeMessage();
  msg.setSender(from);
  msg.setRecipient(to);
  msg.setSubject(subject);
  if (attachment) {
    msg.addMessage({ contentType: "text/plain", data: body });
    msg.addAttachment({
      filename,
      contentType: "text/markdown",
      data: Buffer.from(attachment, "utf8").toString("base64"),
    });
  } else {
    msg.addMessage({ contentType: "text/plain", data: body });
  }
  return msg.asRaw();
}

function b64(s) {
  return btoa(s);
}

/* 行缓冲读取器：从 ReadableStream 读 Uint8Array，按 CRLF 切行 */
class SmtpLineReader {
  constructor(socket) {
    this.reader = socket.readable.getReader();
    this.buf = "";
  }

  async readLine() {
    while (true) {
      const i = this.buf.indexOf("\r\n");
      if (i !== -1) {
        const line = this.buf.slice(0, i);
        this.buf = this.buf.slice(i + 2);
        return line;
      }
      const { done, value } = await this.reader.read();
      if (done) throw new Error("SMTP: connection closed while waiting for line");
      this.buf += new TextDecoder().decode(value, { stream: true });
    }
  }

  async expect(codePrefix) {
    const lines = [];
    let last;
    do {
      last = await this.readLine();
      lines.push(last);
    } while (last.length >= 4 && last[3] === "-");
    const ok = lines.some((l) => l.startsWith(codePrefix));
    if (!ok) throw new Error(`SMTP expected ${codePrefix}, got: ${lines.join(" | ")}`);
    return lines;
  }
}

async function smtpSend({ user, pass, from, to, rawMime }) {
  // secureTransport:"on" = 隐式 TLS（465 端口）。端口 25 被禁止，465/587 可用。
  const socket = connect({ hostname: SMTP_HOST, port: SMTP_PORT }, { secureTransport: "on" });
  const writer = socket.writable.getWriter();
  const encoder = new TextEncoder();
  const write = (str) => writer.write(encoder.encode(str + "\r\n"));
  const writeRaw = (bytes) => writer.write(bytes);
  const reader = new SmtpLineReader(socket);

  try {
    await socket.opened;
    await reader.expect("220");
    write("EHLO mcp-memory-mail");
    await reader.expect("250");
    write("AUTH LOGIN");
    await reader.expect("334");
    write(b64(user));
    await reader.expect("334");
    write(b64(pass));
    await reader.expect("235");
    write(`MAIL FROM:<${from}>`);
    await reader.expect("250");
    write(`RCPT TO:<${to}>`);
    await reader.expect("250");
    write("DATA");
    await reader.expect("354");
    const body = rawMime.replace(/\r?\n/g, "\r\n").replace(/^\./gm, "..");
    writeRaw(encoder.encode(body + "\r\n.\r\n"));
    await reader.expect("250");
    write("QUIT");
    try {
      await reader.expect("221");
    } catch {
      // 服务器可能在 QUIT 后立即关连接
    }
  } finally {
    try {
      writer.close();
    } catch {
      // ignore
    }
    try {
      socket.close();
    } catch {
      // ignore
    }
  }
}

async function mailSend(env, { to, subject, body, filename, attachment }) {
  const user = env.SMTP_USER;
  const pass = env.SMTP_PASS;
  if (!user || !pass) {
    throw new Error("SMTP_USER / SMTP_PASS secrets not set. Run: wrangler secret put SMTP_USER && wrangler secret put SMTP_PASS");
  }
  const from = env.MAIL_FROM || user;
  const toAddr = to || env.MAIL_TO || user; // 不指定收件人则发到默认地址
  const rawMime = buildMailMime({ from, to: toAddr, subject, body, filename, attachment });
  await smtpSend({ user, pass, from, to: toAddr, rawMime });
  return { sent: true, from, to: toAddr, subject };
}

/* --------------------------------- memory --------------------------------- */

async function embed(env, text) {
  const result = await env.AI.run(EMBED_MODEL, { text: [text] });
  const vector = result?.data?.[0];
  if (!Array.isArray(vector)) throw new Error("embedding failed");
  return vector;
}

/* 批量 embedding：免费档单次调用只有 50 个子请求额度，必须按批跑 */
async function embedBatch(env, texts) {
  const result = await env.AI.run(EMBED_MODEL, { text: texts });
  const vectors = result?.data;
  if (!Array.isArray(vectors) || vectors.length !== texts.length) throw new Error("embedding batch failed");
  return vectors;
}

async function memoryInsert(env, { content, tags, machine, tool }) {
  const now = Date.now();
  const rowId = id();
  const tagsJson = JSON.stringify(tags);
  await env.DB.prepare(
    "INSERT INTO memories (id, content, tags, machine, tool, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
  )
    .bind(rowId, content, tagsJson, machine, tool, now, now)
    .run();

  // 语义索引。Vectorize 失败不阻塞写入（FTS 兜底）。
  let semantic = false;
  try {
    const vector = await embed(env, content);
    await env.VEC.upsert([
      { id: rowId, values: vector, metadata: { machine, tool, created_at: now } },
    ]);
    semantic = true;
  } catch (err) {
    console.error(`[memoryInsert] semantic index failed for ${rowId}:`, err?.message || err);
  }
  return { id: rowId, semantic };
}

async function memoryUpdate(env, row, { content, tags, machine, tool }) {
  const now = Date.now();
  const newContent = content ?? row.content;
  const newTags = tags ? JSON.stringify(tags) : row.tags;
  const newMachine = machine ?? row.machine;
  const newTool = tool ?? row.tool;
  await env.DB.prepare("UPDATE memories SET content=?, tags=?, machine=?, tool=?, updated_at=? WHERE id=?")
    .bind(newContent, newTags, newMachine, newTool, now, row.id)
    .run();
  try {
    const vector = await embed(env, newContent);
    await env.VEC.upsert([{ id: row.id, values: vector, metadata: { machine: newMachine, tool: newTool, created_at: row.created_at } }]);
  } catch (err) {
    console.error(`[memoryUpdate] semantic index failed for ${row.id}:`, err?.message || err);
  }
  return { id: row.id, updated: true };
}

/* id 支持短前缀（memory_search/list 只回显前 8 位）。0 条→not found；多条→歧义报错 */
async function resolveMemoryRow(env, idOrPrefix) {
  const rows = rowList(
    await env.DB.prepare("SELECT * FROM memories WHERE id LIKE ?").bind(`${idOrPrefix}%`).all(),
  );
  if (rows.length === 1) return { row: rows[0] };
  if (rows.length === 0) return { error: `memory not found: ${idOrPrefix}` };
  return {
    error: `ambiguous id prefix "${idOrPrefix}" matches ${rows.length} memories: ${rows.map((r) => r.id.slice(0, 8)).join(", ")}`,
  };
}

function parseTags(raw) {
  try {
    const v = JSON.parse(raw);
    return Array.isArray(v) ? v : [];
  } catch {
    return [];
  }
}

function rowToMemory(row) {
  return {
    id: row.id,
    content: row.content,
    tags: parseTags(row.tags),
    machine: row.machine,
    tool: row.tool,
    createdAt: new Date(row.created_at).toISOString(),
    updatedAt: new Date(row.updated_at).toISOString(),
  };
}

async function memorySearch(env, { query, k, machine }) {
  const limit = Math.min(Math.max(k || 8, 1), 50);

  // 混合检索用 RRF（倒数排名）融合：语义分数（0.3~0.7）与 FTS 命中不可直接比大小，
  // 按各自榜单的排名融合才不会出现「精确关键词命中被语义结果挤出前 N」。
  const RRF_K = 60;
  const rrf = new Map(); // id -> 融合分

  // 1) 语义检索
  if (query) {
    try {
      const qv = await embed(env, query);
      const res = await env.VEC.query(qv, { topK: limit, returnMetadata: "all" });
      (res.matches || res || []).forEach((match, i) => {
        if (!match?.id) return;
        rrf.set(match.id, (rrf.get(match.id) || 0) + 1 / (RRF_K + i + 1));
      });
    } catch (err) {
      console.error("[memorySearch] vectorize query failed:", err?.message || err);
    }
  }

  // 2) 关键词检索（FTS5）。MATCH 必须用真实表名（不能起别名）；
  //    每个词单独加引号成短语——裸词里的 `-xxx` 会被 FTS5 解析成列过滤器而报 no such column
  if (query) {
    const terms = query.replace(/["*]/g, " ").split(/\s+/).filter(Boolean);
    if (terms.length) {
      const ftsQuery = terms.map((t) => `"${t}"`).join(" ");
      const rows = await env.DB.prepare(
        `SELECT m.* FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid
         WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?`,
      )
        .bind(ftsQuery, limit)
        .all()
        .catch((err) => {
          console.error("[memorySearch] fts query failed:", err?.message || err);
          return { results: [] };
        });
      rowList(rows).forEach((row, i) => {
        rrf.set(row.id, (rrf.get(row.id) || 0) + 1 / (RRF_K + i + 1));
      });
    }
  }

  // 3) 无 query 或两条路都空 → 退化为最近条目
  if (!rrf.size) {
    const rows = await env.DB.prepare(
      "SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?",
    )
      .bind(limit)
      .all();
    return rowList(rows).map((row) => ({ memory: rowToMemory(row), score: null }));
  }

  // 4) 取回整行 → machine 过滤 → 按融合分排序
  const ids = [...rrf.keys()];
  const placeholders = ids.map(() => "?").join(",");
  const rows = await env.DB.prepare(
    `SELECT * FROM memories WHERE id IN (${placeholders})`,
  )
    .bind(...ids)
    .all();
  const out = [];
  for (const row of rowList(rows)) {
    if (machine && row.machine !== machine) continue;
    out.push({ memory: rowToMemory(row), score: rrf.get(row.id) });
  }
  out.sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
  return out.slice(0, limit);
}

function rowList(all) {
  return all?.results || all || [];
}

/* ---------------------------------- tools --------------------------------- */

function createServer(env) {
  const server = new McpServer({ name: "memory-mail", version: "1.1.0" });

  server.registerTool(
    "mail_send",
    {
      description:
        "Send an email (text/markdown, optional attachment) via QQ SMTP. " +
        "Use for archiving chat sessions or notes. If `to` is omitted, sends to simon4545@qq.com by default.",
      inputSchema: {
        to: z.string().optional().describe("目标收件邮箱地址。不填则默认发给 simon4545@qq.com"),
        subject: z.string().describe("邮件主题"),
        body: z.string().describe("邮件正文（纯文本或 Markdown）"),
        filename: z.string().optional().describe("附件文件名，如 session.md"),
        attachment: z.string().optional().describe("附件内容（文本，如完整会话 Markdown）"),
      },
    },
    async (args) => {
      try {
        const result = await mailSend(env, args);
        return json(result);
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "memory_write",
    {
      description:
        "Write a memory entry (a fact, decision, preference, project state) shared across all machines and AI tools. " +
        "Keep entries atomic: one topic per entry. Returns the memory id. " +
        "To correct an existing entry, prefer the memory_update tool (this tool's id branch rewrites content wholesale).",
      inputSchema: {
        content: z.string().min(1).describe("记忆内容，一句话到一段话，自包含（不要依赖上下文才看得懂）"),
        tags: z.array(z.string()).optional().describe("标签，如 ['project:blog','preference']"),
        machine: z.string().optional().describe("来源机器名，用于溯源"),
        tool: z.string().optional().describe("来源 AI 工具名，如 zcode/cursor/claude-code"),
        id: z.string().optional().describe("要更新的已有记忆 id（支持短前缀）；不填则新建"),
      },
    },
    async (args) => {
      try {
        if (args.id) {
          const found = await resolveMemoryRow(env, args.id);
          if (found.error) return errOut(new Error(found.error));
          return json(await memoryUpdate(env, found.row, args));
        }
        // 近似去重：与最近 200 条做精确/前缀比对交给调用方；这里只做完全相同内容拒绝
        const dup = await env.DB.prepare("SELECT id FROM memories WHERE content=? LIMIT 1").bind(args.content).first();
        if (dup) return json({ id: dup.id, dedup: true });
        return json(await memoryInsert(env, { ...args, machine: args.machine || "", tool: args.tool || "" }));
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "memory_update",
    {
      description:
        "Update an existing memory by id (short id prefix works, e.g. the 8-char id memory_search returns). " +
        "Partial update: only the fields you pass are changed, the rest are kept. " +
        "Use this instead of rewriting via memory_write when correcting an entry.",
      inputSchema: {
        id: z.string().describe("memory id，支持短 id 前缀（如检索返回的 8 位 id）"),
        content: z.string().min(1).optional().describe("新的记忆内容；不传则保持不变"),
        tags: z.array(z.string()).optional().describe("新标签数组（整体替换）；不传则保持不变"),
        machine: z.string().optional().describe("修正来源机器名；不传则保持不变"),
        tool: z.string().optional().describe("修正来源 AI 工具名；不传则保持不变"),
      },
    },
    async (args) => {
      try {
        const fields = ["content", "tags", "machine", "tool"].filter((f) => args[f] !== undefined);
        if (!fields.length) {
          return errOut(new Error("nothing to update: pass at least one of content/tags/machine/tool"));
        }
        const found = await resolveMemoryRow(env, args.id);
        if (found.error) return errOut(new Error(found.error));
        await memoryUpdate(env, found.row, {
          content: args.content,
          tags: args.tags,
          machine: args.machine,
          tool: args.tool,
        });
        const fresh = await env.DB.prepare("SELECT * FROM memories WHERE id=?").bind(found.row.id).first();
        return json({ updated: true, fields, memory: rowToMemory(fresh) });
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "memory_search",
    {
      description:
        "Search shared memories. Semantic search with keyword fallback. Call this at task start when prior context may help.",
      inputSchema: {
        query: z.string().optional().describe("检索词；留空则返回最近条目"),
        k: z.number().optional().describe("返回条数，默认 8，最大 50"),
        machine: z.string().optional().describe("只看某台机器写入的记忆"),
      },
    },
    async (args) => {
      try {
        const hits = await memorySearch(env, args);
        return textOut(
          hits.length
            ? hits
                .map(
                  (h) =>
                    `- [${h.memory.id.slice(0, 8)}] (${h.memory.machine || "?"}/${h.memory.tool || "?"} ${h.memory.createdAt})${h.score != null ? ` score=${h.score.toFixed(3)}` : ""}\n  ${h.memory.content.replaceAll("\n", " ")}`,
                )
                .join("\n")
            : "No memories found.",
        );
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "memory_list",
    {
      description: "List recent memories, oldest/newest first, optionally filtered by tag substring.",
      inputSchema: {
        limit: z.number().optional().describe("条数，默认 20"),
        machine: z.string().optional().describe("按来源机器过滤"),
        tag: z.string().optional().describe("按标签子串过滤"),
      },
    },
    async (args) => {
      try {
        const limit = Math.min(Math.max(args.limit || 20, 1), 200);
        let sql = "SELECT * FROM memories";
        const conds = [];
        const binds = [];
        if (args.machine) {
          conds.push("machine=?");
          binds.push(args.machine);
        }
        if (args.tag) {
          conds.push("tags LIKE ?");
          binds.push(`%${args.tag}%`);
        }
        if (conds.length) sql += ` WHERE ${conds.join(" AND ")}`;
        sql += " ORDER BY updated_at DESC LIMIT ?";
        binds.push(limit);
        const rows = rowList(await env.DB.prepare(sql).bind(...binds).all());
        return textOut(
          rows.length
            ? rows.map((r) => {
                const m = rowToMemory(r);
                return `- [${m.id.slice(0, 8)}] (${m.machine}/${m.tool} ${m.createdAt}) tags=${m.tags.join(",")}\n  ${m.content.replaceAll("\n", " ")}`;
              }).join("\n")
            : "No memories yet.",
        );
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "memory_delete",
    {
      description: "Delete a memory by id (or id prefix).",
      inputSchema: { id: z.string().describe("memory id，可用返回时的短 id 前缀") },
    },
    async (args) => {
      try {
        const rows = rowList(
          await env.DB.prepare("SELECT id FROM memories WHERE id LIKE ?").bind(`${args.id}%`).all(),
        );
        if (!rows.length) return errOut(new Error(`no memory matches: ${args.id}`));
        for (const row of rows) {
          await env.DB.prepare("DELETE FROM memories WHERE id=?").bind(row.id).run();
          await env.VEC.deleteByIds([row.id]).catch(() => {});
        }
        return json({ deleted: rows.map((r) => r.id) });
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "memory_reindex",
    {
      description:
        "Admin: re-embed ALL memories into Vectorize (run after changing EMBED_MODEL or when semantic:false). " +
        "Does not touch updated_at. Safe to re-run.",
      inputSchema: {},
    },
    async () => {
      try {
        const rows = rowList(
          await env.DB.prepare("SELECT id, content, machine, tool, created_at FROM memories").all(),
        );
        const vectors = [];
        const failed = [];
        const BATCH = 10; // 每批 1 个 AI 子请求，控制免费档 50 子请求/次的额度
        for (let i = 0; i < rows.length; i += BATCH) {
          const chunk = rows.slice(i, i + BATCH);
          try {
            const embeddings = await embedBatch(env, chunk.map((r) => r.content));
            chunk.forEach((row, j) => {
              vectors.push({
                id: row.id,
                values: embeddings[j],
                metadata: { machine: row.machine, tool: row.tool, created_at: row.created_at },
              });
            });
          } catch (err) {
            for (const row of chunk) failed.push({ id: row.id, reason: err?.message || String(err) });
          }
        }
        let upserted = 0;
        for (let i = 0; i < vectors.length; i += 50) {
          await env.VEC.upsert(vectors.slice(i, i + 50));
          upserted += Math.min(50, vectors.length - i);
        }
        return json({ total: rows.length, upserted, failed });
      } catch (err) {
        return errOut(err);
      }
    },
  );

  server.registerTool(
    "archive_save",
    {
      description: "Store a large markdown (e.g. a full chat-session transcript) in the archive table.",
      inputSchema: {
        session_id: z.string().describe("会话 id"),
        title: z.string().optional(),
        markdown: z.string().min(1),
        machine: z.string().optional(),
      },
    },
    async (args) => {
      try {
        const rowId = id();
        await env.DB.prepare(
          "INSERT INTO archives (id, session_id, title, markdown, machine, created_at) VALUES (?,?,?,?,?,?)",
        )
          .bind(rowId, args.session_id, args.title || "", args.markdown, args.machine || "", Date.now())
          .run();
        return json({ id: rowId, bytes: args.markdown.length });
      } catch (err) {
        return errOut(err);
      }
    },
  );

  return server;
}

/* ---------------------------------- entry --------------------------------- */

function authorized(request, env) {
  const token = env.MCP_TOKEN;
  if (!token) return { ok: false, reason: "MCP_TOKEN secret is not set on the Worker." };
  const header = request.headers.get("Authorization") || "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  if (match && match[1] === token) return { ok: true };
  return { ok: false, reason: "Unauthorized. Send header: Authorization: Bearer <MCP_TOKEN>" };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return Response.json({ ok: true, smtp: Boolean(env.SMTP_USER), ai: Boolean(env.AI), db: Boolean(env.DB), vec: Boolean(env.VEC) });
    }

    if (url.pathname === "/mcp") {
      if (request.method === "OPTIONS") {
        return new Response(null, {
          status: 204,
          headers: {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "authorization, content-type, mcp-session-id, last-event-id",
          },
        });
      }
      const auth = authorized(request, env);
      if (!auth.ok) {
        return Response.json({ error: auth.reason }, { status: 401 });
      }
      const handler = createMcpHandler(() => createServer(env));
      const response = await handler(request, env, ctx);
      response.headers.set("Access-Control-Allow-Origin", "*");
      return response;
    }

    return new Response("mcp-memory-mail. MCP endpoint: POST /mcp (Bearer auth). Health: /health", {
      status: 404,
    });
  },
};
