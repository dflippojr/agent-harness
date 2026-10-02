// UI harness: replaying a stored taint_added over a session snapshot that already lists it (the stream
// restarts from seq 0 on every view or reconnect) must not duplicate the source (#262 review).
import { withTaint } from "../harness/web/lib/taint.mjs";

const failures = [];
const eq = (label, got, want) => {
  if (JSON.stringify(got) !== JSON.stringify(want)) failures.push(`${label}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};

const snapshot = [{ kind: "web", origin: "evil.example", first_seen: 1 }];
const replayed = { origin: "evil.example", kind: "web" };
eq("replay over snapshot", withTaint(snapshot, replayed), snapshot);
eq("replay twice", withTaint(withTaint(snapshot, replayed), replayed), snapshot);
eq("empty session", withTaint(undefined, replayed), [replayed]);
eq("new origin appends", withTaint(snapshot, { origin: "other.example", kind: "web" }).map((t) => t.origin),
  ["evil.example", "other.example"]);
eq("same origin, other kind is one source", withTaint(snapshot, { origin: "evil.example", kind: "mcp" }), snapshot);

if (failures.length) {
  console.log(failures.join("\n"));
  process.exit(1);
}
console.log("ok");
