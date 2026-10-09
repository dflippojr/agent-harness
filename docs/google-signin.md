# Household member Google sign-in (issue #64)

An owner-provisioned, enabled household member can identify themselves to **bundled, same-origin Agent Harness Web**
with Google OpenID Connect. Tailscale Serve still decides which devices reach the server. Google only says *which
pre-approved member* is at the keyboard. Google never creates an account, never grants access by itself, and never
makes anyone an owner. The feature is **off by default**. Owners, direct-Tailscale members (#62), guests, and app
tokens work exactly as before whether Google is off, misconfigured, or unreachable.

> Tailscale admits this device to the server; Google identifies your pre-approved household account.

## When to use it

#62 members sign in by their exact `Tailscale-User-Login`. That works when each person has their own tailnet login.
Google sign-in covers the other case: a shared household device (a kitchen tablet, a family laptop) whose tailnet
login is not any one member. The owner lists that login under `google_signin.admitted_logins`. A request from that
login has **no role of its own**. It can load the Web shell and the sign-in screen, and nothing else, until a member
signs in with their linked Google account.

## Trust model

- **Network admission is Tailscale.** The daemon listens on loopback only, and `tailscale serve` supplies
  `Tailscale-User-Login`. A Google session is honoured only on a loopback peer that carries that header. Funnel,
  public internet, LAN HTTP, other reverse proxies, and separately hosted Web are not supported.
- **Identity is Google's stable `sub`.** Email and name are display metadata that can change. They never authorize
  anything. Each member has at most one Google `sub`, and each `sub` belongs to at most one member.
- **Same host trust boundary as #62.** The machine owner can read the SQLite database. It holds the member's `sub`,
  display email, and SHA-256 hashes of Web session cookies and link codes. It never holds a token, code, state,
  nonce, PKCE verifier, or the client secret.

### Principal precedence (for owner review)

The orchestrator's launch note asked for the most fail-closed reading, and this table is that reading. It is marked
**for owner review**. "Admitted" means the login is listed in `google_signin.admitted_logins`, which is an explicit
owner allowlist. It does not mean "any tailnet login". With `admitted_logins` empty (the default), Google can only
add a Web session to a member who already authenticates with their own Tailscale login.

| Tailscale login on the request | No Google session | Valid Google session for member M |
| --- | --- | --- |
| absent (localhost) | owner with the local owner token, else refused | **refused** (a cookie without Serve's login never becomes the localhost owner) |
| in `allowed_logins` (owner) | owner | owner (session ignored) |
| mapped to member A | member A | member A if A = M; otherwise member A (session ignored, never switches) |
| mapped to a disabled member | refused | refused |
| active or expired guest | guest / refused | guest / refused (session ignored) |
| in `admitted_logins` | sign-in screen only (401 `sign_in_required`) | member M |
| any other login | refused | refused (session ignored) |
| any login from a non-loopback peer (forged header) | #62 behaviour | session ignored |

The session is also ignored when the request's `Origin` is not the configured `public_url` origin, or when
`Sec-Fetch-Site` is anything other than `same-origin` or `none`. Owner, guest, app, device, runner, and bearer
credentials can never become a member through Google. An `admitted_logins` entry may not also be an owner, guest, or
member login. Account creation and rebind refuse it, and preflight flags any overlap.

## Setup (owner)

### 1. Google Cloud Console

1. Create (or pick) a Google Cloud project. Under **Google Auth Platform → Branding**, set the app name and support
   email.
2. Under **Audience**, choose **External**. While the app is in **Testing**, add each member's Google account as a
   **test user**. Google limits testing apps to 100 test users, and Google may ask a testing app's users to consent
   again periodically. To publish to **Production**, Google requires authorized domains and may require
   verification. Because only the non-sensitive `openid email profile` scopes are used, verification is usually
   light. Google decides whether your `.ts.net` hostname is acceptable as an authorized domain, though, and you
   cannot verify ownership of `ts.net`. If Google refuses it, stay in Testing with named test users, or leave the
   feature off. Never fall back to localhost, wildcard, `http://`, or a public origin.
3. Under **Data Access**, add only `openid`, `.../auth/userinfo.email`, and `.../auth/userinfo.profile`.
4. Under **Clients**, create an **OAuth client ID** of type **Web application**. Add exactly one **Authorized redirect
   URI**:

   ```
   https://<machine>.<tailnet>.ts.net/auth/google/callback
   ```

   It must equal `public_url` + `/auth/google/callback`. Use the same port if `public_url` has one. **Actions →
   Accounts → Google sign-in** shows the exact value to copy. Leave **Authorized JavaScript origins** empty, because
   the browser never talks to Google's APIs directly.
5. Download the client JSON (or copy the client secret).

### 2. Client secret file

Store the secret in a file **outside the repository**, for example under the data directory. The file can be the
downloaded `client_secret_*.json` (its `client_id` and `redirect_uris` are checked against your config), or a file
holding just the secret on one line.

- **Linux/macOS:** owned by the daemon account, mode `600` (`chmod 600 file`).
- **Windows:** the ACL must not grant Everyone, Users, Authenticated Users, Guests, Anonymous, Interactive, or
  Network. A file under the daemon account's profile or a restricted data folder normally qualifies. To lock it down
  explicitly:

  ```powershell
  icacls D:\Agents\harness\secrets\google-client.json /inheritance:r /grant:r "$env:USERNAME:(R)" "SYSTEM:(F)" "Administrators:(F)"
  ```

The daemon reads the file only to check it and to exchange a code. It never copies the secret into SQLite, YAML,
settings APIs, the browser, logs, or diagnostics. Never commit it.

### 3. Configuration (`harness.local.yaml`)

```yaml
public_url: https://tower.your-tailnet.ts.net    # must be the HTTPS Tailscale Serve origin
allowed_logins: [you@example.com]                # required before any member exists
google_signin:
  enabled: true
  client_id: 1234567890-abc.apps.googleusercontent.com
  client_secret_file: D:/Agents/harness/secrets/google-client.json
  admitted_logins: [kitchen-tablet@example.com]  # shared-device tailnet logins; no role of their own
```

Restart the daemon. Then open **Actions → Accounts**. The **Google sign-in** card runs the preflight and lists anything
that keeps the feature off:

- `enabled` is set, `public_url` is `https://<machine>.<tailnet>.ts.net[:port]` with no path, and `listen.host` is
  loopback.
- `allowed_logins` is set, `client_id` looks like a Google web client ID, and `admitted_logins` overlaps no owner or
  guest.
- The secret file exists, is a regular file (not a link), is outside the source tree, and has the permissions above.
- Google's discovery document fetches and validates: the exact issuer, Google-hosted HTTPS endpoints, PKCE `S256`,
  and `RS256`.

`GET /api/admin/v1/google-signin?refresh=true` returns the same preflight. Until it passes, the sign-in button is
hidden and sign-in requests return 503. Existing Web sessions keep working through a Google outage.

## Enrolling a member

A member's Google account must be linked to their household account before it can sign in. There are two ways:

- **Self-link.** The member opens **Profile → Account** from a device with their own Tailscale login and chooses **Link
  Google account**.
- **Owner link code.** In **Actions → Accounts**, the owner chooses **Google link code** for that member. The code
  carries 256 bits of randomness, is shown once, works once, and expires after 15 minutes. Only its hash is stored. It
  is never put in a URL, QR code, log, or audit event. The owner gives it to the member privately. On the admitted
  device the member enters it on the sign-in screen and completes Google sign-in. Creating a new code, cancelling it,
  disabling the member, or redeeming it invalidates the code.

Owners cannot start a Google authorization as a member. To change Google accounts, unlink first, then link again.

## Sign-in flow

The flow is the server-side authorization code flow with PKCE `S256`, `openid email profile`, `access_type=online`,
and `prompt=select_account`. It has no incremental authorization, refresh tokens, One Tap, implicit or device flow,
or UserInfo call.

1. `POST /api/v1/auth/google/start` (same-origin only, with the link code in the body if there is one) binds a
   10-minute in-memory attempt. The attempt records the mode, the member (for link and code flows), the browser (an
   `HttpOnly __Host-` attempt cookie), the Tailscale login, high-entropy `state` and `nonce`, and the PKCE verifier.
2. Google redirects to `/auth/google/callback`. The attempt is consumed (single use). The server then checks `state`,
   the deadline, the optional RFC 9207 `iss` parameter (a mix-up defence), and that the Tailscale login is unchanged.
   It exchanges the code server-side and verifies the ID token. The checks are: the RS256 signature against Google's
   rotation-aware JWKS cache, the exact issuer, the audience, `azp`, `exp`/`iat` with 60 seconds of skew, the nonce,
   and `email_verified`. The code, access token, and ID token are discarded.
3. The member's session is created, and any earlier session in that browser is revoked. The browser is redirected
   with a generic result. Failures never reveal whether a Google account, code, `sub`, email, or member exists.

The uvicorn access log redacts the callback's query string.

## Web sessions

- The cookie is `__Host-ah_session`, with `Secure; HttpOnly; SameSite=Lax; Path=/` and no `Domain`. Its value is 256
  random bits, and SQLite stores only its SHA-256, the member `user_id`, timestamps, and revocation.
- Sessions expire after 7 days idle or 30 days absolute. Last-seen writes are throttled to one every 5 minutes.
- Every cookie-authenticated mutation must send an exact same-origin `Origin`, `Sec-Fetch-Site: same-origin`, and the
  per-session `X-Agent-Harness-CSRF` value. Web gets that value from `GET /api/v1/auth/session` and keeps it in memory
  only.
- Admission and precedence are checked again on every request and stream. Logout, unlink, owner revoke-all, member
  disable or rebind, and a new link all revoke sessions immediately and close the member's live streams. Re-enabling
  a member does not bring sessions back.
- Sessions survive a daemon restart. An in-flight sign-in does not, and fails with the generic error.

**Bundled Web only.** The cookie works only on the `public_url` origin. Separately hosted Web or Apps cannot use it.
Cross-origin requests ignore the cookie, and no member bearer token is ever placed in local or session storage or a
URL. A separately hosted member session would need a later cross-origin, BFF, or partitioned-cookie design.

## API

| Route | Who | Purpose |
| --- | --- | --- |
| `GET /api/v1/auth/session` | anyone through Serve | whether Google is offered here, signed-in state, CSRF value (to its own session only) |
| `POST /api/v1/auth/google/start` | admitted device or member | `{"mode": "signin" \| "link" \| "invite", "code"?}` → `authorization_url` |
| `GET /auth/google/callback` | Google redirect | completes the flow and redirects to Web |
| `POST /api/v1/auth/logout` | session holder | revokes this session |
| `GET /api/v1/me/google` | member | linked email, dates, active session count |
| `DELETE /api/v1/me/google` | member | `{"confirm": true}`: unlink and end all Google Web sessions |
| `GET /api/admin/v1/google-signin[?refresh=true]` | owner | readiness, preflight, exact redirect URI |
| `POST /api/admin/v1/accounts/{id}/google/invitation` | owner | create or replace a one-time link code (shown once) |
| `DELETE /api/admin/v1/accounts/{id}/google/invitation` | owner | cancel the link code |
| `POST /api/admin/v1/accounts/{id}/google/revoke-sessions` | owner | end every Google Web session for the member |
| `DELETE /api/admin/v1/accounts/{id}/google` | owner | `{"confirm": true}`: unlink |

Owner account views include a coarse `google` object (`linked`, `email`, `linked_at`, `last_sign_in_at`,
`active_web_sessions`, `invitation_expires_at`). They never include the `sub`, claims, tokens, or hashes. Audit rows
record the action, the outcome, and opaque IDs only.

## Recovery and revocation

- **Lost device or suspected misuse.** **Revoke Web sessions** for the member, or remove the device's login from
  `admitted_logins` and restart.
- **Member changed Google accounts.** Unlink (owner or member), then self-link or issue a new link code.
- **Member disabled.** Their sessions and any pending code end immediately. Their linked identity stays for
  re-enable, and they sign in again after re-enable.
- **Unlink keeps everything else.** Member data and their direct Tailscale login are untouched. Web warns a member
  who unlinks from a Google-only device that this device will stop opening their account.
- **Turning the feature off.** Set `google_signin.enabled: false` and restart. Cookies are then ignored, and #62
  behaviour returns unchanged.

## Not included

Self-sign-up, Google owner login, Google API access, refresh or offline tokens, other identity providers,
public/Funnel/LAN access, native CLI login, Agent Harness Apps, separately hosted Web member sessions, account
merge/transfer/deletion, and multiple Google identities per member. Browser-level end-to-end tests against real
Google are also not included: the automated tests use a mocked Google (discovery, JWKS, token endpoint), and the
Console checklist above is the owner's manual verification.
