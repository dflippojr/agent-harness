// Compile-only contract tests. No runtime imports or generated code are served.
/** @import { Api } from './contracts.js' */

/** @param {Api} api @param {string} id */
export async function checkContracts(api, id) {
  const list = await api("/sessions?limit=10");
  const session = await api(`/sessions/${id}`);
  /** @type {string} */
  const title = session.title;
  /** @type {string | undefined} */
  const preview = list[0]?.chat_summary;
  /** @type {number | null | undefined} */
  const position = session.queue_position;
  // @ts-expect-error A renamed API field must not become an unchecked string.
  const wrong = session.session_title.toUpperCase();
  // @ts-expect-error Session detail is an object, not a list.
  session.map(() => "");
  const jobs = await api("/jobs");
  /** @type {string} */
  const cron = jobs[0].cron;
  // @ts-expect-error Wrong job fields cannot be used as strings.
  jobs[0].schedule.toUpperCase();
  const discovery = await api("");
  /** @type {string} */
  const path = discovery.operations[0].path;
  // @ts-expect-error The discovery contract declares path, not url.
  discovery.operations[0].url.toUpperCase();
  /** @type {null} */
  const deleted = await api(`/jobs/${id}`, { method: "DELETE" });
  return { title, preview, position, wrong, cron, path, deleted };
}

/** @param {import('../../harness/web/client.mjs').AgentHarnessWebClient} client @param {string} id */
export async function checkTransport(client, id) {
  const session = await client.request(`/sessions/${id}`, { surface: "app" });
  /** @type {string} */
  const title = session.title;
  // @ts-expect-error The actual transport must preserve the generated field types.
  session.session_title.toUpperCase();
  const created = await client.request("/sessions", { method: "POST", body: { prompt: "Test" }, surface: "app" });
  /** @type {string} */
  const createdId = created.id;
  // @ts-expect-error POST /sessions returns one session, not the GET session list.
  created.map(() => "");
  return { title, createdId };
}
