#!/usr/bin/env node
/**
 * Single MCP tool call: node tools/mcp-call.mjs <endpoint> <token> <tool> [jsonArgs]
 * 例：node tools/mcp-call.mjs https://.../mcp "$TOKEN" memory_reindex '{}'
 * 本机直连 workers.dev 不通时，先起代理：node tools/socks2http.mjs 10809 10808，
 * 再加 NODE_USE_ENV_PROXY=1 HTTPS_PROXY=http://127.0.0.1:10809。
 */
const [endpoint, token, tool, argsRaw] = process.argv.slice(2);
if (!endpoint || !token || !tool) {
  console.error("usage: node tools/mcp-call.mjs <endpoint> <token> <tool> [jsonArgs]");
  process.exit(2);
}

let sessionId = null;
let rpcId = 0;

async function rpc(method, params, notification = false) {
  const body = { jsonrpc: "2.0", method, params };
  if (!notification) body.id = ++rpcId;
  const res = await fetch(endpoint, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      ...(sessionId ? { "mcp-session-id": sessionId } : {}),
    },
    body: JSON.stringify(body),
  });
  const sid = res.headers.get("mcp-session-id");
  if (sid) sessionId = sid;
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("text/event-stream")) {
    const raw = await res.text();
    for (const line of raw.split("\n")) {
      if (line.startsWith("data:")) {
        try {
          const evt = JSON.parse(line.slice(5).trim());
          if (!notification && evt.id === rpcId) return evt.result;
        } catch {}
      }
    }
    return null;
  }
  if (res.status === 202) return null;
  return (await res.json()).result;
}

await rpc("initialize", {
  protocolVersion: "2025-06-18",
  capabilities: {},
  clientInfo: { name: "mcp-call", version: "1.0.0" },
});
await rpc("notifications/initialized", {}, true);

const result = await rpc("tools/call", {
  name: tool,
  arguments: argsRaw ? JSON.parse(argsRaw) : {},
});
if (!result) {
  console.error("no result");
  process.exit(1);
}
for (const c of result.content || []) process.stdout.write(c.text);
if (result.isError) process.exit(1);
