# Study: `vercel-labs/json-render` for Agent Harness Web

Research date: 2026-09-30. Issue: #175. Research only; nothing here adds a dependency or changes Web behavior.

## Recommendation

**Ignore the framework today; keep the closed-catalog idea on file.** No current Agent Harness Web surface needs
model-authored layout, and the framework is a React-oriented, build-step, npm-dependent stack that does not fit a
no-build static PWA. If a surface appears later, build a small purpose-built closed schema (sketch below), not
`json-render`. "Ignore" is a complete outcome; no follow-up implementation issue is proposed.

## Upstream, as reviewed

| Fact | Value | Source |
|---|---|---|
| Repo | `vercel-labs/json-render`, not archived | https://github.com/vercel-labs/json-render |
| Version / commit | `@json-render/core` 0.21.0 (tag v0.21.0, 2026-09-18); `main` at `c2600d73908ed505e6d726f5b6f969ba8f597ce7` (2026-09-23) | GitHub API, retrieved 2026-09-30 |
| Maintenance | Last push 2026-09-30, last commit on main 2026-09-23, monthly releases (v0.20.0 2026-08-18), ~18.4k stars. Pre-1.0, so API churn is expected. | GitHub API |
| License | Apache-2.0 | repo `LICENSE`, GitHub API `license.spdx_id` |
| Model | Developer defines a catalog (component name, Zod props schema, description, named actions). The model emits a JSON spec (element map with `type`, `props`, `children`, optional `visible`, `repeat`). Specs are validated against the catalog; renderers map catalog names to components. | README; `packages/core/src/{schema,spec-validator,actions,props}.ts` |
| Packages | ~25 under `packages/`: `core`, `react`, `vue`, `svelte`, `solid`, `react-native`, `shadcn`, `react-pdf`, `react-email`, `remotion`, `image`, `ink`, `mcp`, state adapters, devtools | repo tree |
| Dependencies | `core` depends on `zod ^4`. `react` needs `react ^19.2`. No vanilla-DOM renderer. | `packages/*/package.json` |
| Streaming | "SpecStream" compiles streamed patches into a progressively growing spec. | README |
| Built-in actions | `setState`, `navigate: <string>`, `set`, plus catalog-defined custom actions | `packages/core/src/actions.ts` |

I did not find a `SECURITY.md` or a published security model beyond "the model can only use catalog components and actions."
Source reading was limited to the files listed above; this is not an audit.

## Agent Harness Web today

From `docs/web.md` and `harness/web`:

- Static files (`index.html`, `app.js`, `client.mjs`, `style.css`, `sw.js`), **no build step**, served by Agent Harness Server
  or any HTTPS origin. Hash-routed (`#/s/<id>`, `#/profile/...`), vanilla DOM via an `h()` helper in `app.js`.
- API: `/api/v1` and `/api/admin/v1`; the service worker caches only the static shell.
- Model text goes through `md()` (`mdLinkAt` accepts only `http://`/`https://` links) and is then assigned with `innerHTML` via
  the `html` prop of `h()` (`app.js:138`). Audited call sites with model-derived HTML: `app.js:1154`, `2214`, `2363`; search
  highlights at `1316`. `md()` has an XSS regression harness (`tests/test_web_markdown*.py`, `tests/web_markdown_cases.json`).
- Approvals are rendered from server-side structured data (`approvalWhat`, `app.js:~296`) and decided through
  `POST /api/v1/.../approvals/<id>`. Chat snippet results render as plain text and the model cannot start a run.
- **No Content-Security-Policy** is set anywhere in `harness/` (searching `harness/*.py` for `Content-Security-Policy` and
  `frame-ancestors` found nothing). Today's XSS defence is only the discipline of `md()`.

## Candidate surfaces

| Surface | Existing pattern | Would model-generated UI help? |
|---|---|---|
| Final answer / transcript | `md()` into chat bubble | Markdown already covers headings, tables, lists, links. Marginal. |
| Approval cards | server-built from tool-call data | **No.** Must stay deterministic and server-derived; model-authored text beside an approve button is a spoofing risk. |
| Job / review / usage dashboards | fixed views fed by the admin API | No. Data shapes are known; fixed views are simpler and testable. |
| Search results | server passages + highlights | No. |
| Structured answers (comparison table, checklist, status summary) | tables in `md()` | Possible, but a Markdown table already does it. |

Conclusion: no surface has a need that Markdown plus fixed views does not meet, so generated UI is not warranted now.

## Threat boundary

Inputs the model sees include repository files, web pages, and tool output, all attacker-influenceable. Treat every spec as
attacker-controlled.

**What catalog/schema validation enforces:** the component `type` is one of the allowed names; props have the declared types;
the tree is structurally well-formed (`spec-validator.ts` checks references and visibility shapes); named actions exist.

**What it does not enforce** (a `string` is a valid `string`):

- *HTML/script injection.* A `Text` prop is a valid string whatever it contains. Safety depends entirely on the renderer using
  `textContent`/DOM construction. Routing any spec value through `h({html})` / `innerHTML` (including via `md()` output) defeats
  the catalog. A catalog renderer must never use the `html` prop.
- *URLs.* Upstream's `navigate` is `z.string()`, and a catalog `href`/`src` prop is whatever its author declares.
  `javascript:`, `data:`, `//host`, and exfiltration URLs (an image `src` carrying secrets in the query) all pass a plain string
  schema. Constraint: parse with `new URL`, allow only `https:` (and `http:` only if `md()` parity is wanted), enforced at render time.
  Disallow remote images, since a load is an exfiltration channel.
- *Action execution.* Schemas validate the action name and param shape, not whether the user intended it. `set`/`setState`
  with `z.unknown()` values mutate client state that other bound props read. Constraint: generated UI may carry **no** actions
  except purely local view state (expand/collapse). No action may call the API, start a run, send a message, or answer an
  approval. Approvals stay server-rendered through the existing `approvalWhat` path and policy checks; generated content
  must never sit inside, or be mistaken for, an approval card.
- *Spoofing and resource use.* Valid specs can mimic system UI or be huge or deeply nested. Constraints: label the region as
  model-generated, cap node count, depth, and string length, and render in a bordered container with no fixed/overlay positioning.
- *Streaming.* A partial spec is untrusted; render a node only once it validates.

**Prerequisite regardless of approach:** add a CSP (`default-src 'self'`, no inline script, `img-src 'self'`,
`frame-ancestors 'none'`) as defence in depth. It does not replace renderer discipline and is a separate change.

## Framework vs. purpose-built closed schema

| | `json-render` | Small closed schema |
|---|---|---|
| Fit with a no-build vanilla PWA | Poor: renderers exist for React/Vue/Svelte/Solid only; `core` gives validation, not rendering | Native: a small validator plus DOM builders in the existing `h()` style |
| Dependencies | `zod` 4 in core, React 19 for the main renderer, and a bundler to ship them. This breaks the no-build property and the service-worker shell model | None |
| Supply chain | Pre-1.0, fast release cadence, 25-package monorepo; each upgrade needs review | Reviewed in-repo |
| Features | Streaming, state binding, repeat, conditions, actions | Only what is needed; no state or actions |
| Safety surface | Larger: `$state`, `$bindState`, `navigate`, `set` must all be constrained away | Small by construction |
| Validation | Zod, mature | Hand-written; needs a test corpus like the `md()` one |

Using only `@json-render/core` with a vanilla renderer would still need a bundler (or a vendored build) for the Zod import and
would use almost none of the framework, so it gives little over a purpose-built schema.

### Minimal schema sketch (if ever needed)

```json
{"v":1,"root":[{"t":"heading","text":"..."},{"t":"text","text":"..."},{"t":"list","items":["..."]},
  {"t":"table","cols":["..."],"rows":[["..."]]},{"t":"badge","text":"...","tone":"ok|warn|err"},
  {"t":"link","text":"...","href":"https://..."}]}
```

- Component allowlist: `heading`, `text`, `list`, `table`, `badge`, `link`, `details` (local expand/collapse). No `image`, `html`, `button`, `form`, `iframe`.
- Props: closed set per type; unknown keys are rejected, not ignored. Strings are rendered with `textContent` only, max 2,000 characters.
- URLs: `link.href` only, `https:` via `new URL`, `rel="noopener noreferrer"`, `target="_blank"`, host displayed.
- No actions, state, or expressions. Limits: 200 nodes, depth 4.
- Failure mode: an invalid spec falls back to the raw text via `md()`. This is falsifiable: any spec with a key or value outside the lists must be rejected, testable with a corpus.

## Proof of concept: not run

- No concrete surface was identified where structured UI beats existing Markdown or fixed views, which is the issue's condition for running one.
- No local model endpoint answered on the default Ollama / llama.cpp ports (`localhost:11434`, `localhost:8080`) on this host during the study.
- Therefore **no reliability claim is made** about local models producing valid specs, parse-failure rates, or streaming behavior. If a surface is later
  chosen, run N>=30 samples per prompt against a named local runtime and record model/configuration, catalog, prompts, parse and validation failures, and streaming observations.

## Limitations

- Upstream security properties are inferred from public source and the README, not audited; `json-render` was not run.
- Upstream figures (stars, activity) are point-in-time as of 2026-09-30.
- #18 (smart approvals), #170, and #174 are context only. If smart approvals ever show a model-written rationale, it should stay plain text.
