#!/usr/bin/env node
/**
 * Tiny SOCKS5 -> HTTP proxy converter for wrangler.
 * Listens on 127.0.0.1:HTTP_PORT (default 10809) and tunnels via
 * a local SOCKS5 proxy (default 127.0.0.1:10808).
 * Supports CONNECT tunnels (what wrangler/undici needs) and plain absolute-form GET/POST.
 *
 * Usage: node tools/socks2http.mjs [httpPort] [socksPort]
 */
import http from "node:http";
import net from "node:net";

const HTTP_PORT = Number(process.argv[2] || 10809);
const SOCKS_PORT = Number(process.argv[3] || 10808);

const swallow = (sock) => {
  if (!sock || typeof sock.on !== "function") return;
  sock.on("error", () => {
    if (typeof sock.destroy === "function") sock.destroy();
  });
};

function socksConnect(host, port) {
  return new Promise((resolve, reject) => {
    console.log(`[socks2http] connect ${host}:${port}`);
    const isIp = net.isIP(host);
    const sock = net.connect({ host: "127.0.0.1", port: SOCKS_PORT });
    let stage = "greeting";
    let done = false;
    const fail = (err) => {
      if (done) return;
      done = true;
      console.error(`[socks2http] fail ${host}:${port}:`, err?.message || err);
      sock.destroy();
      reject(new Error(String(err?.message || err)));
    };
    sock.setTimeout(15000, () => fail(new Error("socks timeout")));
    sock.on("error", fail);
    sock.on("connect", () => sock.write(Buffer.from([5, 1, 0])));
    sock.on("data", function onData(chunk) {
      console.log(`[socks2http] stage=${stage} reply=${chunk.toString("hex").slice(0, 24)}`);
      if (stage === "greeting") {
        if (chunk[0] !== 5 || chunk[1] !== 0x00) return fail(new Error("socks: greeting failed"));
        stage = "request";
        if (isIp) {
          const ip = host.split(".").map(Number);
          sock.write(Buffer.from([5, 1, 0, 1, ...ip, port >> 8, port & 0xff]));
        } else {
          const hostBuf = Buffer.from(host, "utf8");
          sock.write(
            Buffer.concat([
              Buffer.from([5, 1, 0, 3, hostBuf.length]),
              hostBuf,
              Buffer.from([port >> 8, port & 0xff]),
            ]),
          );
        }
        return;
      }
      sock.off("data", onData);
      if (chunk[1] !== 0x00) return fail(new Error(`socks connect failed: code ${chunk[1]}`));
      if (done) return;
      done = true;
      sock.setTimeout(0);
      sock.off("error", fail);
      sock.off("close", () => {});
      swallow(sock);
      resolve(sock);
    });
  });
}

const server = http.createServer(async (req, res) => {
  swallow(res.socket);
  try {
    const url = new URL(req.url);
    const upstream = await socksConnect(url.hostname, Number(url.port) || 443);
    const lines = [`${req.method} ${url.pathname}${url.search} HTTP/1.1`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      if (/^proxy-/i.test(req.rawHeaders[i])) continue;
      lines.push(`${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}`);
    }
    upstream.write(lines.join("\r\n") + "\r\n\r\n");
    req.pipe(upstream);
    upstream.pipe(res);
  } catch (err) {
    if (!res.headersSent) res.writeHead(502);
    res.end(String(err.message || err));
  }
});

server.on("connect", async (req, socket, head) => {
  swallow(socket);
  console.log(`[socks2http] CONNECT ${req.url}`);
  const idx = req.url.lastIndexOf(":");
  const host = req.url.slice(0, idx);
  const port = Number(idx >= 0 ? req.url.slice(idx + 1) : 443) || 443;
  try {
    const upstream = await socksConnect(host, port);
    socket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head?.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  } catch (err) {
    socket.end("HTTP/1.1 502 Bad Gateway\r\n\r\n");
  }
});

swallow(server);

process.on("uncaughtException", (err) => {
  console.error("[socks2http] swallowed:", err.message);
});
process.on("unhandledRejection", (err) => {
  console.error("[socks2http] rejection:", String(err?.message || err));
});

server.listen(HTTP_PORT, "127.0.0.1", () => {
  console.log(`HTTP proxy on 127.0.0.1:${HTTP_PORT} -> SOCKS5 127.0.0.1:${SOCKS_PORT}`);
});
