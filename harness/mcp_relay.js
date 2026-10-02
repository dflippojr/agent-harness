// MCP relay sidecar (#300). Listens on loopback inside the session's network namespace and hands each HTTP request
// to the daemon as one JSON line on stdout; the daemon answers with one JSON line on stdin. Run as
// `node -e <this file> <port>` (the port is the last argument, so `node mcp_relay.js <port>` works too). It holds no
// token and makes no decisions: the daemon checks everything.
const http = require("http");
const readline = require("readline");

const rawPort = process.argv.at(-1);
const port = /^[0-9]+$/.test(rawPort) ? Number(rawPort) : NaN;
if (!(port >= 1 && port <= 65535)) {
  process.stderr.write(`mcp relay: invalid port ${JSON.stringify(rawPort)}; expected an integer 1-65535\n`);
  process.exit(2);
}
const MAX_BODY = 4 * 1024 * 1024;
const waiting = new Map();
let next = 0;

const send = (message) => process.stdout.write(JSON.stringify(message) + "\n");

const server = http.createServer((req, res) => {
  const chunks = [];
  let size = 0;
  req.on("data", (chunk) => {
    size += chunk.length;
    if (size > MAX_BODY) {
      res.writeHead(413).end();
      req.destroy();
      return;
    }
    chunks.push(chunk);
  });
  req.on("end", () => {
    if (size > MAX_BODY) return;
    const id = ++next;
    waiting.set(id, res);
    res.on("close", () => waiting.delete(id));
    send({ id, method: req.method, path: req.url, headers: req.headers,
           body: Buffer.concat(chunks).toString("utf8") });
  });
});

readline.createInterface({ input: process.stdin }).on("line", (line) => {
  let reply;
  try {
    reply = JSON.parse(line);
  } catch (e) {
    return;
  }
  const res = waiting.get(reply.id);
  if (!res) return;
  waiting.delete(reply.id);
  res.writeHead(reply.status || 500, reply.headers || {});
  res.end(reply.body || "");
}).on("close", () => process.exit(0));

server.listen(port, "127.0.0.1", () => send({ ready: port }));
