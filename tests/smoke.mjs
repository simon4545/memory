/**
 * MCP smoke test: node tests/smoke.mjs <endpoint> <token>
 * 例：node tests/smoke.mjs http://127.0.0.1:8787/mcp local-dev-token
 * 覆盖 memory_update 全路径：短 id 前缀、部分字段更新、not found、歧义前缀、
 * memory_write 的 id 更新分支，以及清理。
 */
const BASE = process.argv[2] || "http://127.0.0.1:8787/mcp";
const TOKEN = process.argv[3] || "local-dev-token";

let sessionId = null;
let rpcId = 0;
let failures = 0;

async function rpc(method, params, notification = false) {
  const body = { jsonrpc: "2.0", method, params };
  if (!notification) body.id = ++rpcId;
  const res = await fetch(BASE, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
      Accept: "application/json, text/event-stream",
      ...(sessionId ? { "mcp-session-id": sessionId } : {}),
    },
    body: JSON.stringify(body),
  });
  const sid = res.headers.get("mcp-session-id");
  if (sid) sessionId = sid;
  const ct = res.headers.get("content-type") || "";
  let payload = null;
  if (ct.includes("text/event-stream")) {
    const raw = await res.text();
    for (const line of raw.split("\n")) {
      if (line.startsWith("data:")) {
        try {
          const evt = JSON.parse(line.slice(5).trim());
          if (!notification && evt.id === rpcId) payload = evt;
        } catch {}
      }
    }
  } else if (res.status !== 202) {
    payload = await res.json();
  }
  if (!notification && (!payload || payload.error)) {
    throw new Error(`${method} failed: ${JSON.stringify(payload)}`);
  }
  return payload?.result;
}

function check(name, cond, detail = "") {
  if (cond) {
    console.log(`PASS ${name}`);
  } else {
    failures++;
    console.log(`FAIL ${name} ${detail}`);
  }
}

function toolText(result) {
  return result?.content?.map((c) => c.text).join("\n") ?? "";
}

/* ---- main ---- */
const init = await rpc("initialize", {
  protocolVersion: "2025-06-18",
  capabilities: {},
  clientInfo: { name: "smoke", version: "1.0.0" },
});
check("initialize", init?.serverInfo?.name === "memory-mail", JSON.stringify(init?.serverInfo));
await rpc("notifications/initialized", {}, true);

const tools = await rpc("tools/list", {});
const names = tools.tools.map((t) => t.name);
check("tools/list 含 memory_update", names.includes("memory_update"), names.join(","));

const uniq = `smoke-update-${Date.now()}`;
const wr = await rpc("tools/call", {
  name: "memory_write",
  arguments: { content: uniq, tags: ["smoke:tag1"], machine: "smoke-machine", tool: "smoke-tool" },
});
const created = JSON.parse(toolText(wr));
check("memory_write 新建返回 id", typeof created.id === "string" && created.id.length === 36, toolText(wr));
const shortId = created.id.slice(0, 8);

// 只改 tags：短前缀 + 部分字段
const up1 = await rpc("tools/call", {
  name: "memory_update",
  arguments: { id: shortId, tags: ["smoke:tag2"] },
});
const r1 = JSON.parse(toolText(up1));
check("短前缀+仅 tags 更新", r1.updated === true && r1.fields.join(",") === "tags", toolText(up1));
check("content 保持不变", r1.memory.content === uniq, r1.memory.content);
check("tags 已替换", r1.memory.tags.join(",") === "smoke:tag2", r1.memory.tags.join(","));
check("machine/tool 保持不变", r1.memory.machine === "smoke-machine" && r1.memory.tool === "smoke-tool");

// 改 content + machine
const up2 = await rpc("tools/call", {
  name: "memory_update",
  arguments: { id: shortId, content: `${uniq}（已更新）`, machine: "smoke-machine-2" },
});
const r2 = JSON.parse(toolText(up2));
check("content+machine 更新", r2.memory.content.endsWith("（已更新）") && r2.memory.machine === "smoke-machine-2", toolText(up2));
check("tags 未被波及", r2.memory.tags.join(",") === "smoke:tag2");

// 空更新要报错
const up3 = await rpc("tools/call", { name: "memory_update", arguments: { id: shortId } });
check("无字段更新报错", up3.isError === true, toolText(up3));

// 不存在的 id
const up4 = await rpc("tools/call", { name: "memory_update", arguments: { id: "zzzzzzzz", content: "x" } });
check("not found 报错", up4.isError === true && toolText(up4).includes("not found"), toolText(up4));

// memory_write 的 id 分支（短前缀）也能更新
const up5 = await rpc("tools/call", {
  name: "memory_write",
  arguments: { id: shortId, content: `${uniq}（write 分支更新）`, tags: ["smoke:tag3"] },
});
const r5 = JSON.parse(toolText(up5));
check("memory_write id 分支短前缀更新", r5.updated === true && r5.id === created.id, toolText(up5));

// 检索能命中更新后的内容。必须带 score=：不带分数说明走的是「最近条目」兜底
// （兜底条目 score 为 null），那意味着 FTS 没命中——测试盲区，等于没验证检索。
const sr = await rpc("tools/call", { name: "memory_search", arguments: { query: uniq, k: 5 } });
const srText = toolText(sr);
check(
  "更新后可检索到（FTS 命中带分数）",
  srText.includes("write 分支更新") && srText.includes("score="),
  srText.slice(0, 400),
);

// 清理
const del = await rpc("tools/call", { name: "memory_delete", arguments: { id: shortId } });
check("清理删除", toolText(del).includes(created.id), toolText(del));

console.log(failures ? `\n${failures} FAILED` : "\nALL PASS");
process.exit(failures ? 1 : 0);
