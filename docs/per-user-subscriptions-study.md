# Per-user CLI subscriptions: provider terms and display-only login (#377, #365 step 1)

Docs-only study. Nothing was signed in, created or accepted to write it: every fact below comes from public pages read
on **2026-10-05**. Pinned CLIs (`sandbox/cli.Dockerfile`): Claude Code 2.1.272 and Codex 0.154.0. Cursor Agent is not pinned there (the
image runs Cursor's install script); the version checked is the one `docs/phase8a-design.md` recorded in the image on
2026-10-04, 2026.10.01-e373342, and a rebuild may differ.

**How the sources were read.** Pages were fetched through a summarising fetch tool, so quoted passages are the tool's
extraction, not a byte-for-byte copy. Where a page could not be read (HTTP 403) the row says so and the point is
**unverified**. This is a research note, not legal advice, and the owner should read the primary pages before
committing to a verdict that carries risk (the links are in each section).

## Verdict (the v1 backend list for #365)

| Backend | Terms allow per-user, third-party-App use? | Login display-only? | Verdict |
| --- | --- | --- | --- |
| Claude Code | **No / unclear.** Anthropic forbids third parties to "collect, store, or intermediate" claude.ai credentials; hosting requires Commercial Terms | **No.** The code pasted into the CLI is relayed by the harness | **Out of v1** |
| Codex | **Unclear.** No prohibition found, no explicit permission either; the OpenAI Terms of Use page could not be read | **Yes.** `codex login --device-auth` shows a URL and a user code | **Out of v1 until the owner answers one question** (below). Best candidate to enter |
| Cursor | **Unclear.** Terms bar renting or lending the service; nothing on CLI or third-party apps | **Yes** (URL only, `NO_OPEN_BROWSER=1`); the CLI's server-side logout behaviour is undocumented | **Out of v1 until the owner answers one question** (below). Second candidate |

The owner expected Codex and Cursor to qualify and Claude not to. **The study confirms the login half of that and
corrects the terms half:** the login flows split exactly as expected, but for Codex and Cursor the terms are silent
rather than permissive, and the rule for this study is that unclear counts as out. #365 therefore has **no confirmed v1
backend** until the owner resolves the open points. Everything else in #365 can proceed with Codex first.

### Open points for the owner

1. **Codex:** read OpenAI's Terms of Use (https://openai.com/policies/terms-of-use/, which returned 403 to this study)
   and the Codex help article, and decide whether "each subscriber signs in to their own Codex on a machine you
   administer, driven through your App" is acceptable. If a written answer from OpenAI support is wanted, that is the
   question to ask. Answer yes and Codex enters v1.
2. **Cursor:** same question against https://cursor.com/terms-of-service. Also confirm with Cursor whether
   `agent logout` revokes the grant server-side.
3. **Claude:** only revisit if the owner signs Anthropic's Commercial Terms (the route Anthropic documents for hosting
   Claude Code) *and* a display-only login appears. Neither is expected soon.

## 1. Consumer and plan terms

### Claude Code

Sources: https://code.claude.com/docs/en/legal-and-compliance and https://www.anthropic.com/legal/consumer-terms
(Consumer Terms effective 2025-10-08), read 2026-10-05.

- One plan, one subscriber. Consumer Terms: "You may not share your Account login information, Anthropic API key, or
  Account credentials with anyone else."
- Automation. Consumer Terms bar access "through automated or non-human means, whether through a bot, script, or
  otherwise" except "via an Anthropic API Key or where we otherwise explicitly permit it". The Claude Code docs
  supply the permission for the CLI itself (CI and scripts, `claude -p`, `setup-token`), so headless CLI use by the
  subscriber is permitted by Anthropic's own docs.
- Third-party application. The legal page: "Anthropic does not permit third-party developers to offer Claude.ai
  login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of
  their users. Moreover, developers may not collect, store, or intermediate Claude.ai credentials or session tokens —
  sign-in to a Claude account must complete through Anthropic's own flow."
- The carve-out. Same page: nothing "prevent[s] an end user from signing in to the unmodified Claude Code binary with
  their own Claude subscription, including where a platform hosts Claude Code". But hosting Claude Code in a product
  "requires agreeing to our Commercial Terms of Service" and keeping to two conditions: the binary is unmodified, and
  "Customers may not pay for, resell, or intermediate Claude usage on their end users' behalf. Each end user must
  authenticate with their own Anthropic API key, Claude subscription plan credentials, or 3P inference provider
  credential."
- Limits assume "ordinary, individual usage of Claude Code and the Agent SDK".

Reading: the shape the owner wants (each person signs in to their own plan in the unmodified CLI, on a hosted
platform) is the shape the carve-out describes, **but** it is conditioned on Commercial Terms the harness has not
signed, and the credential must never be collected, stored or relayed by the harness.

### Codex

Sources: https://developers.openai.com/codex/auth (now https://learn.chatgpt.com/docs/auth), search-result excerpts
of the OpenAI Terms of Use (personal terms effective 2026-01-01) and the Codex help article
(https://help.openai.com/en/articles/11369540), read 2026-10-05. The Terms of Use page itself returned 403, so these
clauses are second-hand.

- One plan, one subscriber. Terms of Use (second-hand): "You may not share your account credentials or make your
  account available to anyone else and are responsible for all activities that occur under your account."
- Automation. The auth docs: "API keys are still the recommended default for automation." ChatGPT sign-in is
  supported for the CLI, and device auth is "the preferred method" on headless machines. The Terms of Use bar
  "automated or programmatic" extraction of data or output (second-hand); whether running the official CLI counts is
  not addressed.
- Third-party application: **not addressed** in anything read. Help article: when signed in with a ChatGPT account,
  the ChatGPT Terms of Use apply to data shared between Codex and ChatGPT.

### Cursor

Sources: https://cursor.com/terms-of-service ("Last updated September 3, 2026") and
https://cursor.com/docs/cli/reference/authentication, read 2026-10-05.

- Account. "You are solely responsible for maintaining the confidentiality of your account and password, and you
  accept responsibility for all activities that occur under your account." No explicit no-sharing sentence surfaced.
- Third parties. "You may not: rent, lease, lend, or sell the Service" or "knowingly permit any third party to do any
  of the foregoing." Also bars harvesting, scraping or extracting data from the Service.
- Automation and CLI: the terms say nothing specific; the docs offer `CURSOR_API_KEY` "for CI/automation scenarios".
- Third-party application: **not addressed.**

## 2. Shared machine

None of the three sources forbids several subscribers' CLI logins on one machine that one person administers. Claude
Code documents separate accounts side by side through separate `CLAUDE_CONFIG_DIR`s (authentication docs, "Log in
with multiple accounts"). What the terms do constrain is *who holds the credential* and *whose plan pays*: Anthropic
requires each end user to use their own plan and forbids the platform from collecting or relaying the login; the
other two bar sharing credentials.

The harness does not yet isolate per-person logins for two of the three. Per `docs/phase8a-design.md` ("Per-domain CLI
state (#371)"), only Codex logs in per domain; the Claude and Cursor login volumes (`harness-login-claude`,
`harness-login-cursor`) are shared by every domain. Per-person Claude or Cursor logins would need per-person login
volumes. That is #365 work, and a precondition for using any verdict above.

## 3. Login flows at the pinned versions

| CLI | Flow | Display-only? | Notes |
| --- | --- | --- | --- |
| Claude Code 2.1.272 | `claude auth login` (browser). Where the browser cannot reach the local callback (WSL2, SSH, containers) it "shows a login code instead of redirecting back" and the user must "paste it into the terminal at the `Paste code here if prompted` prompt" | **No.** The pasted code is a credential the harness would relay | Matches `docs/phase8a-design.md`, Login |
| Codex 0.154.0 | `codex login --device-auth`: URL plus user code, finishes on OpenAI's site. Needs device-code login enabled in the user's ChatGPT security settings | **Yes.** The user code is meant to be displayed and is not a credential | |
| Cursor Agent 2026.10.01 | `agent login`; `NO_OPEN_BROWSER=1` prints the URL | **Yes** (URL only; the login completes in the user's browser) | |

Long-lived tokens:

- **Claude `setup-token`** is covered in its own section below.
- **Codex:** `~/.codex/auth.json` can be copied to a headless machine ("treat ... like a password: it contains access
  tokens"), and tokens refresh automatically. Copying it is a credential hand-off, so it is **not** display-only and
  not the harness's route. Workspace-managed access tokens exist for enterprise automation.
- **Cursor:** `CURSOR_API_KEY` from the dashboard (API billing, not the subscription login). The harness already
  treats this as the `api_key` provider policy.

A newer CLI version was not checked beyond current docs; the docs read above are current as of the read date.

## 4. Revocation and logout

- **Claude:** `/logout` clears the local login. The docs say `/logout` "removes and revokes the credential" for the
  Console keyless sign-in; for claude.ai logins they do not say the server-side grant is revoked. A feature request
  (anthropics/claude-code#25185) reports no way to see or revoke where Claude Code is signed in. **Server-side
  revocation: unconfirmed.**
- **Codex:** `codex logout` removes stored credentials (ChatGPT OAuth tokens, API keys). Third-party documentation
  does not say it revokes the grant server-side. **Unconfirmed.**
- **Cursor:** `agent logout` "Sign[s] out and clear[s] stored authentication"; the docs are silent on server-side
  revocation. **Unconfirmed.**

For all three, the harness can only promise to delete the user's login volume. The user should also be told to remove
the CLI's authorisation from their provider account settings if they want the grant revoked; that wording belongs in
#365's user-facing text.

## 5. Claude `setup-token` for harness sessions (agent-harness #390)

**Question:** is the long-lived token permitted and suitable for the harness's headless hosted sessions?

What it is (authentication docs, "Generate a long-lived token", read 2026-10-05): `claude setup-token` runs "the same
browser authorization flow as `/login`" and prints "a one-year OAuth token". It "does not save the token anywhere".
You set it as `CLAUDE_CODE_OAUTH_TOKEN`. It "authenticates with your Claude subscription and requires a Pro, Max, Team,
or Enterprise plan. It can only make model requests, so it can't establish Remote Control sessions or fetch claude.ai
connectors." It is documented "for CI pipelines and scripts where browser login isn't available". `--bare` mode does
not read it. It ranks fifth in the credential precedence list, above `/login` subscription credentials.

**Suitable for #390: yes, technically.** It is a fixed, inference-only credential with no refresh, so concurrent
sessions cannot race a refresh-token rotation or overwrite a shared file with an empty login. Caveats the
implementation must handle:

- Precedence: `CLAUDE_CODE_OAUTH_TOKEN` outranks the login volume, so the volume can be dropped from sessions.
- Features needing the claude.ai login (connectors, Remote Control, `/schedule`) will not work in those sessions.
- One year lifetime: the harness needs an expiry reminder and `-Status` should report validity without printing it.
- `setup-token` "[enforces] only `forceLoginMethod`", so it can mint a token for a different organisation than
  `forceLoginOrgUUID`; the owner must sign in to the intended account.

**Permitted: yes for the owner's own account driving the owner's own sessions; no for anyone else's.**

- The owner's own use. Anthropic documents the token for headless scripts and CI, which is the "explicitly permit"
  the Consumer Terms require for automated access, and nothing forbids a person keeping their own credential in their
  own secret store. This is the permitted case for the harness as the owner's tool.
- Anyone else's. The legal page forbids third-party developers to "route requests through Free, Pro, or Max plan
  credentials on behalf of their users" and to "collect, store, or intermediate" claude.ai credentials. A token that a
  user minted and handed to the harness is exactly a stored claude.ai credential. So `setup-token` must **not** be
  the per-user mechanism, and no user other than the owner may have their sessions authenticated by the owner's token.
- **Owner decision flagged:** #390 describes a financial-planner *App* session running on this token. While the owner
  is the only user (as noted in the project memory) that is the owner's own use. Once members or App end users drive
  sessions on the owner's Max plan, it becomes "routing requests through plan credentials on behalf of users", which
  the legal page prohibits. Moving to API-key billing or Commercial Terms is the documented way to serve other people.
- Plan limits assume "ordinary, individual usage"; a long-lived token feeding many concurrent sessions may hit them
  sooner. Not a terms issue, but worth watching.

**Recommendation for #390:** option 1 (`setup-token`, `CLAUDE_CODE_OAUTH_TOKEN`, read-only or no login volume) is
permitted for the owner's own use and fixes the race. Keep it owner-only, and record that it must be replaced by API
keys (or a Commercial Terms agreement) before any other person's sessions run on it.

## Source list (all read 2026-10-05)

- https://code.claude.com/docs/en/legal-and-compliance
- https://code.claude.com/docs/en/authentication
- https://www.anthropic.com/legal/consumer-terms
- https://support.claude.com/en/articles/13189465-logging-in-to-your-claude-account (no revocation information)
- https://learn.chatgpt.com/docs/auth (was developers.openai.com/codex/auth)
- https://help.openai.com/en/articles/11369540 (via search excerpt)
- https://openai.com/policies/terms-of-use/ (403; second-hand via search excerpts)
- https://cursor.com/terms-of-service
- https://cursor.com/docs/cli/reference/authentication and /parameters
