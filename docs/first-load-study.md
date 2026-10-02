# First-load study: non-Chat pages after a fresh deploy (issue #188)

Analysis only. No product code was changed. Recommendations are filed as follow-up issues (see the end).

## Question

Non-Chat pages feel slower on the first visit after a deploy. Is that a caching problem, a blocking-fetch problem, or
something else, and does it bear on the first-load splash (#179)?

## Conditions

| Item | Value |
|---|---|
| Browser | Playwright-bundled Chromium 153.0.8010.12 (Playwright 1.63.0), headless |
| Desktop profile | 1280x800, no throttling |
| Mobile profile | Emulation only (no physical device): 390x844, DPR 3, touch |
| Mobile throttled profile | Same emulation, 4x CPU throttle, and a model of a slow link applied **at a local TLS proxy**: +150 ms per request and 200 KB/s downstream (about 1.6 Mbps). It does not model TLS handshakes or connection setup. |
| Server | Isolated local instance built from `main` at `abcc524`: its own temp config and data root, full profile with images, jobs and GPU guard enabled (GPU/ComfyUI backends absent), no projects, no sessions, no jobs, no images. The normal local daemon was never touched. |
| Transport | Local TLS proxy with a throwaway self-signed certificate (Chromium launched with `--ignore-certificate-errors`). The service worker only registers on `https:` (`harness/web/app.js`, `location.protocol === "https:"`), so plain-http runs have no service worker; an early plain-http run was discarded for that reason. |
| Repetitions | 5 per route and state; the table cells are median (min-max) in ms |

**States**

- **Cold**: fresh browser context (no cache, no service worker), first navigation.
- **Warm**: the same context after the service worker is controlling, then a reload.
- **Update**: a context whose service worker was installed from a different `BUILD_ID` (the shipped worker rewritten
  to an old ID for the first visit), then a reload against the real build. This is the closest scripted model of
  "first visit after a deploy" for a returning user.

**Metrics** (measured in page via `PerformanceObserver` and `MutationObserver`)

- *Shell*: `first-contentful-paint`.
- *Usable content*: first route-specific element inside `#app`: session rows or the empty state (`#/agents`),
  `textarea`/form (`#/new`), job rows or empty state (`#/jobs`), the prompt textarea (`#/images`), the GPU action row
  (`#/actions/gpu`), the identity card (`#/profile`). The route's data fetches complete before any of these render.

**Routes**: sessions list (`#/agents`), New task (`#/new`), Jobs (`#/jobs`), Image (`#/images`), Actions
(`#/actions/gpu`), Profile (`#/profile`). "Tasks" covers both `#/new` and `#/jobs`. Chat as a comparison route was
**not** measured: with no backend the instance cannot open a meaningful chat (see Limitations).

## Results

### Desktop 1280x800 (no throttling)

| Route | State | FCP ms | Usable ms |
|---|---|---|---|
| sessions | cold | 36 (28-40) | 67 (58-94) |
| sessions | warm | 32 (20-40) | 45 (38-58) |
| sessions | update | 24 (20-36) | 50 (42-55) |
| new-task | cold | 32 (28-44) | 68 (65-96) |
| new-task | warm | 28 (20-48) | 46 (44-73) |
| new-task | update | 32 (20-36) | 47 (44-71) |
| jobs | cold | 36 (28-44) | 54 (53-106) |
| jobs | warm | 24 (20-36) | 39 (37-48) |
| jobs | update | 20 (20-36) | 39 (35-52) |
| image | cold | 28 (28-36) | 75 (70-80) |
| image | warm | 20 (20-32) | 46 (45-50) |
| image | update | 32 (20-36) | 54 (48-57) |
| actions | cold | 36 (28-44) | 56 (53-65) |
| actions | warm | 28 (20-36) | 49 (36-50) |
| actions | update | 20 (20-32) | 35 (33-80) |
| profile | cold | 28 (28-32) | 65 (57-69) |
| profile | warm | 20 (20-36) | 41 (38-60) |
| profile | update | 32 (20-36) | 42 (40-97) |

### Mobile 390x844 DPR 3 (no throttling)

| Route | State | FCP ms | Usable ms |
|---|---|---|---|
| sessions | cold | 32 (28-44) | 70 (61-85) |
| sessions | warm | 20 (20-36) | 49 (45-63) |
| sessions | update | 24 (20-36) | 44 (41-53) |
| new-task | cold | 40 (28-40) | 70 (68-75) |
| new-task | warm | 32 (24-40) | 51 (47-65) |
| new-task | update | 24 (24-36) | 50 (49-56) |
| jobs | cold | 32 (28-44) | 67 (60-71) |
| jobs | warm | 20 (20-36) | 43 (37-52) |
| jobs | update | 32 (20-36) | 40 (36-53) |
| image | cold | 36 (28-48) | 101 (81-121) |
| image | warm | 36 (32-104) | 57 (51-131) |
| image | update | 24 (20-40) | 70 (46-72) |
| actions | cold | 40 (32-44) | 65 (53-68) |
| actions | warm | 32 (24-36) | 55 (42-62) |
| actions | update | 36 (24-36) | 47 (36-55) |
| profile | cold | 32 (28-48) | 64 (60-83) |
| profile | warm | 24 (20-36) | 50 (38-75) |
| profile | update | 32 (20-36) | 46 (41-55) |

### Mobile throttled (4x CPU, +150 ms RTT, 200 KB/s)

| Route | State | FCP ms | Usable ms |
|---|---|---|---|
| sessions | cold | 492 (484-516) | 2763 (2748-2790) |
| sessions | warm | 448 (444-456) | 2729 (2711-2739) |
| sessions | update | 444 (440-456) | 2725 (2711-2761) |
| new-task | cold | 488 (480-492) | 3109 (3071-3152) |
| new-task | warm | 448 (444-584) | 2913 (2879-3043) |
| new-task | update | 460 (444-460) | 2902 (2843-2923) |
| jobs | cold | 496 (484-500) | 2762 (2746-2897) |
| jobs | warm | 444 (444-460) | 2726 (2706-2738) |
| jobs | update | 444 (440-448) | 2713 (2694-2742) |
| image | cold | 488 (472-504) | 2961 (2922-3105) |
| image | warm | 452 (444-460) | 2913 (2889-2928) |
| image | update | 460 (444-492) | 2899 (2894-2933) |
| actions | cold | 492 (480-500) | 2601 (2583-2796) |
| actions | warm | 444 (440-448) | 2554 (2546-2565) |
| actions | update | 444 (444-456) | 2554 (2543-2568) |
| profile | cold | 504 (496-508) | 2766 (2750-2871) |
| profile | warm | 484 (444-500) | 2772 (2662-2789) |
| profile | update | 480 (444-508) | 2782 (2746-2785) |

### Server restart (isolated instance, desktop, unthrottled)

Five restarts of the isolated instance, each followed by a first cold load of `#/jobs`: 131-157 ms wall-clock to
usable content, versus 106-118 ms for an immediate reload on the same instance (this harness's wall-clock timer
includes about 100 ms of automation overhead and is not comparable to the in-page numbers above). A fresh server
adds roughly 25-40 ms for the first request; not a meaningful contributor.

### Waterfall of one throttled load (jobs route; request start to response end, ms)

| Request | Cold | Warm |
|---|---|---|
| `style.css`, `icon-192.png` | 202-469 | 190-475 |
| `app.js?v=5` (258,704 bytes, uncompressed) | 203-1752 | 191-1761 |
| `client.mjs` | 1755-1911 | 1763-1921 |
| `/health` | 1935-2095 | 1925-2091 |
| `/me` | 2100-2267 | 2095-2260 |
| `/gpu`, `/profile` (parallel) | 2273-2440 | 2264-2433 |
| `/models/warm`, second `/me` (parallel) | 2444-2608 | 2437-2605 |
| `/jobs` (route data) | 2614-2777 | 2611-2771 |

## Findings

**Confirmed by measurement**

1. On an unthrottled local link every route is interactive in well under 150 ms in every state. Cold costs roughly
   20-30 ms more than warm. Nothing in these numbers looks like a slow first visit.
2. On the throttled link the first visit takes about 2.6-3.1 s to usable content and shell paint is about 0.45-0.5 s.
   Warm and update visits are only 30-200 ms faster, so the "first visit after a deploy" penalty is **not** a
   cache-miss penalty: the warm path is almost as slow as the cold one.
3. In the waterfall the time is two things. About 1.5 s is the `app.js` download (258,704 bytes, served uncompressed;
   gzip of the same file is 70,183 bytes). The remaining roughly 1 s is a serial chain of requests that only start
   after the previous step completes: `app.js` -> `client.mjs` -> `/health` -> `/me` -> `/gpu` + `/profile` ->
   `/models/warm` + second `/me` -> the route's own data. Each hop costs one round trip.
4. Warm loads re-download `app.js` and `style.css` in full. The service worker fetches shell files with
   `cache: "no-cache"` (`harness/web/sw.js`), so the cache is only the offline fallback by design. In this setup those
   fetches went out without an `If-None-Match` validator and returned `200` with the full body every time (checked at
   the proxy), even though the server sends an `ETag`. Warm is therefore not meaningfully cheaper than cold. Browser
   check outside Playwright: not run (owner decision, 2026-10-01); the `StaticFiles` mounts return `304` for a
   matching `If-None-Match`, which a test now guards. `/`, `/sw.js` and `/manifest.webmanifest` have their own
   `FileResponse` routes that send an `ETag` but always answer `200` (Starlette does not evaluate conditionals there);
   not changed, per the owner decision.
5. Routes differ little from one another (2.6-3.1 s throttled). The dispatcher gates every route on the same shared
   start-up chain, so per-route data (`/sessions`, `/queue`, `/gpu`, `/projects`, `/images`, `/jobs`) is a small part.
   New task is the slowest (about 3.1 s cold) because it adds `/projects`, `/models` and `/gpu` fetches after the
   shared chain.
6. The shell already has a boot splash (`harness/web/index.html`, dismissed in `app.js` after the first route has
   rendered, 4 s failsafe). #179 is closed.

**Hypotheses (not measured)**

- Compressing static files (about 70 KB instead of 259 KB) would cut roughly 1 s off the throttled first load. Only the
  gzip size was measured; no compressed serving was timed.
- Issuing `/health`, `/me`, `/gpu` and `/profile` in parallel, or starting them from a small inline script before
  `app.js` finishes, would remove most of the serial round trips. Not prototyped.
- The missing conditional request on warm loads may be specific to Playwright's browser contexts; a normal Chromium
  profile may revalidate and return `304`. The browser check was not run (owner decision, 2026-10-01, #290); no
  behavior change was made. The server side is regression-tested (`tests/test_webgzip.py::test_shell_revalidates_with_304`).
- If the field complaint is mostly about the first request after a server restart or deploy window, this study did not
  reproduce it: server restart added only tens of milliseconds locally.
- Real devices on real cellular or Tailscale paths add TLS/connection setup and path variance that this model omits.

**What is *not* supported by the evidence**

- "Caching makes first visit slow." Warm and cold are nearly equal under throttling; cache state is not the lever.
- A per-route cause for any single route. The routes share the same start-up cost.

## Does this bear on #179?

#179 (splash) is closed and a splash exists. Measured first-load latency is dominated by shell download and the serial
start-up chain, which a splash covers visually but does not shorten. Latency is a reason to keep the splash's
current bounded, instant-removal behaviour (it ends when the first route has rendered), not a reason to reopen splash or
skeleton work. Any reduction belongs to the follow-up issues below.

## Limitations

- **Staging slot not measured.** The staging runner is not reachable from this machine, and its profile fails sessions
  closed and disables image, jobs and GPU guard, so it can only give shell-only timings. Everything here is from an
  isolated local instance.
- **Chat not measured.** No backend is configured on the isolated instance, so Chat was not run as the comparison.
- **No data.** There are no sessions, jobs or images, so "usable content" for lists is the empty state or the form.
  Large lists would add rendering and payload time that this study did not measure.
- **Network is modelled, not real.** Throttling is a fixed delay plus bandwidth cap at a local proxy; the CDP network
  emulation was rejected because it does not apply to service-worker fetches and made warm runs look artificially fast.
- **Emulated mobile**, headless Chromium only. No Safari/WebKit (the primary phone browser for this app) and no real
  device.
- Five repetitions per cell; medians are stable within a few percent, but ranges are narrow partly because the
  environment is quiet.

## Follow-up issues

Recommendations are filed as separate issues rather than implemented here:

- #288: compress static assets (finding 3; the compression saving is a hypothesis).
- #289: shorten the serial start-up request chain (finding 3; the saving is a hypothesis).
- #290: verify warm-load revalidation outside Playwright (finding 4; possibly an automation artifact). Browser check
  not run by owner decision; only the server-side 304 regression test was added.

## Reproducing

The harness was a short Playwright script outside the repository: per route it opens a fresh context for the cold
run, reloads for the warm run, and for the update run installs a service worker from a rewritten `BUILD_ID` first. The
in-page observers record `first-contentful-paint` and the time the route marker element first appears. Throttled runs go
through a small local TLS proxy that delays each request by 150 ms and limits the downstream rate to 200 KB/s.

## Follow-up: gzip for the web shell (#288)

The daemon now gzips the shell at runtime (`harness/webgzip.py`): `/static/*`, root-mounted assets, `/` and `/sw.js`, text
types only, for clients that send `Accept-Encoding: gzip`. API JSON, SSE, images and package downloads are untouched.
Compressed responses carry `Vary: Accept-Encoding` and a weak ETag, so `If-None-Match` still returns 304 for both
variants. Hosting `harness/web` from a plain static host at its root is out of scope; that host's own compression applies.

Transferred bytes measured from the real app (identity vs gzip), with the slow-link model (200 KB/s) applied analytically:

| Asset | Identity | gzip | Transfer time at 200 KB/s |
| --- | --- | --- | --- |
| `/static/app.js` | 268,244 | 72,746 | 1.34 s -> 0.36 s |
| `/static/style.css` | 34,978 | 7,935 | 0.17 s -> 0.04 s |
| `/` | 4,099 | 1,478 | 0.02 s -> 0.01 s |

The shell drops by about 225 KB, i.e. roughly 1.1 s of transfer time on the modelled link; the per-request +150 ms RTT is
unchanged. This is a byte-count model, **not** a re-run of the Playwright first-load script (that harness lives outside
the repo), so the ~1 s first-load saving is supported by the arithmetic but not re-measured end to end.

## Follow-up: app.js split into ES modules, stage (a) (#258)

Stage (a) moved the pure helpers into nine `harness/web/lib/*.mjs` modules, so a cold load fetches 11 scripts instead of
2 (`app.js`, `client.mjs`, `lib/`). Measured with `scripts/web-first-paint.mjs` (in repo; headless Chromium 153.0.8010.12
over CDP, cold profile per run, 390x844 DPR 3, 4x CPU, +150 ms per request, 200 KB/s, gzip like `harness/webgzip.py`,
`/api` answering 503 so the route renders its offline state). 5 runs, median (min-max) ms, route `#/agents`:

| Bundle | FCP | Module graph executed (DOMContentLoaded end) | JS requests |
| --- | --- | --- | --- |
| `main` at `301e61f` (before) | 480 (476-496) | 957 (933-960) | 2 |
| stage (a) | 488 (488-496) | 1142 (1123-1159) | 11 |
| stage (a) + `<link rel=modulepreload>` for `lib/` and `client.mjs` (not shipped) | 480 (468-504) | 961 (940-993) | 11 |

First paint (the shell and boot splash) does not regress: the module graph is deferred and does not block it. The app
code starts about 185 ms later on the throttled cold model, because the `lib/` imports are only discovered after `app.js`
arrives (one extra round trip; `markdown.mjs -> snippets.mjs` adds no visible second one). Preload hints in `index.html`
recover it fully. Per the #258 decision (preload only if first paint regresses) they are not added; the numbers are here
so the trade-off can be revisited as `pages/` modules land. Warm and update loads were not re-measured: the service
worker is network-first and only falls back to its cache offline, so they make the same requests as a cold load. This
is not the out-of-repo Playwright script
used above, so compare the rows in this table with each other, not with the earlier tables.
