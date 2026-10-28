# GitHub CI and container images

`.github/workflows/ci.yml` is the single test workflow, and `.github/workflows/ci-cd.yml` publishes and deploys only
after that workflow succeeds for the exact commit pushed to `main`:

1. **CI / test** runs the complete test suite once, with coverage, on GitHub-hosted `windows-latest` for pull
   requests and pushes to `main`, and uploads `coverage.xml` as an artifact. **CI / sonar** (same workflow,
   `needs: test`) feeds that coverage to SonarCloud and waits for the quality gate. A failing gate fails the check on
   pull requests and fails the CI run on a push to `main`, so it blocks deployment just as failing tests do (#728).
   Tests used to run a second time on a self-hosted `agent-harness-ci` pool on the
   tower; that pool is retired so CI no longer competes with llama-server for the tower's RAM.
   **CI / gate** is the one check to require (#726). It always runs, needs every other CI job, and fails if any of
   them failed or was cancelled, `sonar` included on a push to `main`. **CI / gate-guard** (#727) calls the shared gate
   guard from dflippojr/.github on each pull request, pinned by commit SHA (`guard-ref` names the same commit). It is an
   early signal inside `gate`, not the judge, because a pull request supplies this file and could replace the job. The
   judge is `.github/workflows/gate-guard-target.yml` (#727): it runs from `main` under `pull_request_target`, calls
   the same shared guard pinned to the same commit, and its own job, `gate-guard-target / gate-guard`, is the verdict on each pull request's head. It
   fails a pull request that deletes or skips a test, adds a retry, or edits workflow, Sonar, coverage or lint config,
   unless the owner applied `gate-change-approved` after the newest push. **CI / changes** lists the
   changed files; when every one is on its `CI_SKIP_UNREAD_PATHS` list, `test`, `test-py313`, `review-linux`, `openapi`,
   `ui-tests`, `hub-contract` and `deploy-linux` skip and
   `gate` still passes, so a docs-only PR is not left pending. A push to `main` is never cancelled by a later push:
   each main commit gets its own CI run, and only pull-request runs cancel their older run.
   **CI / lint** (#729) runs on every change, on `ubuntu-latest`: `ruff check` with the small rule set in
   `pyproject.toml` (`F`, `E9`, `B`, `PLE`; no formatter or import sorting), then pyright in basic mode
   (`pyrightconfig.json`) over `harness`, `harness_modules`, `sdk` and `macrunner`. Pyright fails only on errors that
   `tools/lint/pyright-baseline.json` does not record; fixing a recorded one and rewriting the baseline shrinks it.
   **CI / hub-contract** (#736) runs the Hub's contract suite (dflippojr/agent-harness-hub `tests/contract`, its
   `e2e`-marked `[daemon]` cases), checked out at the full SHA pinned in the job, against this commit's daemon in
   fake-model mode on loopback, on `ubuntu-latest`. A daemon change that renames a field, changes a status code or
   drops an endpoint the Hub calls fails it; AGENTS.md describes the deliberate-break flow (Hub PR first, then bump
   the pinned ref).
   **CI / deploy-linux** (#812) runs `tests/test_deploy_ci_linux.py` on GitHub-hosted `ubuntu-latest`, which ships
   PowerShell 7: `ops/harness/deploy-ci.ps1` under `pwsh` with fake git, docker and systemctl (stop, start, rollback,
   reruns), and `install/run-daemon.sh` with a fake daemon. The Windows suite jobs skip that module. It never runs on
   the tower's `agent-harness-tower-linux` runner, which deploys `main` and never runs pull request code.
   **CI / review-linux** (#806) runs `tests/test_review_runner.py` and `tests/test_runner_install.py` on GitHub-hosted
   `ubuntu-latest`, which ships PowerShell 7 and Pester: `ops/review/run-review.ps1`, its Pester suites and
   `ops/github/install-runner.sh` on Linux, where the review runners live. `REVIEW_TESTS_REQUIRE_PWSH=1` turns a
   missing `pwsh` into a failure rather than a skip. No coverage; `test` measures it.
   **CI / test-py313** (#730) runs the same suite on Python 3.13, on `windows-latest`, after an import smoke test
   (`tests/test_import_smoke.py`) that compiles every shipped source file and imports every shipped module with
   `SyntaxWarning` as an error. It has no coverage. `gate` needs it, so a failure fails the CI run, and `guard` deploys
   only from a successful run.
2. **guard** checks the `CI` run first: it continues only when that run's `test` job ran and passed (a docs-only
   push skips it) and the commit is still `main`'s tip. Main runs can finish out of order, so this stops an older
   commit from publishing images or deploying after a newer one; the newer commit's own run deploys. `guard` runs
   outside any concurrency group, so an older run cannot replace a newer commit's queued deployment; only
   `publish-images` (`agent-harness-main-publish`) and `deploy-tower` (`agent-harness-main-deployment`) serialize.
   `publish-images` repeats the tip check as its first step, inside its group, because the guard's answer can go
   stale while the job waits there; a stale run publishes nothing, and its `deploy-tower` skips.
   **publish-images** is triggered by `workflow_run` only after `CI` completes successfully for a push to `main`.
   It checks out `github.event.workflow_run.head_sha`, never a branch name, and GitHub-hosted Linux builders publish
   both runtime images to GHCR:
   - `ghcr.io/dflippojr/agent-harness-sandbox:sha-<commit>` and `:py312`
   - `ghcr.io/dflippojr/agent-harness-cli:sha-<commit>` and `:1`
3. **deploy-tower** runs only after image publication for that same tested SHA. The repository-scoped runner on the
   Ubuntu tower (`agent-harness-tower-linux`, #812) runs `ops/harness/deploy-ci.ps1` under `pwsh`. It pulls the
   immutable images, verifies that the live checkout is clean `main` and can fast-forward, builds a fresh side-by-side
   virtual environment, stops the daemon (`systemctl --user stop agent-harness-main-daemon.service`, as the runner's
   user), swaps environments, fast-forwards, retags the images, starts the daemon, and waits for `/health`.

The SHA tags are immutable deployment inputs; the stable tags match the local names already expected by the daemon.
The CLI Dockerfile accepts `SANDBOX_IMAGE` so its hosted build is based on the exact sandbox image from the same commit.
The supervisor reads the ignored `.venv-path` file when present. Deployment switches that pointer rather than moving
the newly built environment, so entry points keep their original absolute paths; the original environment remains
untouched for first-deployment rollback. Later successful deployments remove the superseded managed environment.
On Linux the daemon's unit runs the installer's supervisor (`~/.local/share/agent-harness/bin/run-daemon.sh`, a copy
of `install/run-daemon.sh`), and the original environment is the installer's `~/.local/share/agent-harness/venv`.
The daemon's unit is a systemd user unit of the runner's user, so the deployment needs no sudo. A runner service has
no user session, so when `XDG_RUNTIME_DIR` is unset the script sets it to `/run/user/$(id -u)`, which is how
`systemctl --user` reaches the user manager (it needs no `DBUS_SESSION_BUS_ADDRESS`). Before downtime it checks that
the user unit is installed and runs that supervisor for the live checkout, and stops, naming the problem, otherwise;
while the daemon is stopped it replaces the supervisor with the release's `install/run-daemon.sh`, and a rollback
restores the previous one. The Hub, llama-server and metrics proxy units are not touched.
Deploys (production and staging) install `requirements.txt` plus, when present, `requirements-telemetry.txt`, so tracing
works in production; `requirements-repomap.txt` stays opt-in.

Both builds read any available GitHub Actions cache, but cache export uses `ignore-error=true`. A `workflow_run` token
may be unable to write the default branch's Actions cache; that optional optimization must never block GHCR publication
or deployment. No later step consumes the exported cache. `publish-images` receives `contents: read` and
`packages: write`; `deploy-tower` receives only `contents: read`.
The GHCR packages are public (this repository is public), so the tower pulls the immutable `sha-<commit>` images
anonymously and does not sign in. The first production deployment showed why: `docker login ghcr.io` with the job's
`GITHUB_TOKEN` was rejected (`denied: denied`) on the tower, which failed the deployment before anything was changed.

## Docs regeneration on main

`.github/workflows/docs-regen.yml` regenerates regions after changes to fragments, generator code or Server code
on `main` (and on manual dispatch). It is the single writer; feature PRs add fragments only. Changed regions land
through a PR from `bot/docs-regen`, never a direct push to `main` (#738).

1. The dedicated `dflippojr-docs-regen` App mints a repository-scoped installation token using
   `actions/create-github-app-token` and the repository Actions secrets `DOCS_REGEN_APP_ID` and
   `DOCS_REGEN_APP_PRIVATE_KEY`. Its installation is limited to agent-harness, with Contents and Pull requests write.
2. The workflow force-pushes generated regions to `bot/docs-regen` and opens or reuses its PR using that token.
   The branch commit has no skip marker, so the App's PR events start normal `pull_request` CI. Manually dispatched
   CI cannot satisfy required PR checks and is not used. The default `GITHUB_TOKEN` only reads workflow runs.
3. It waits for the CI run on that exact head SHA and requires a successful `gate` job. It then polls the check-runs
   API for `gate-guard-target / gate-guard` on the same SHA (the newest run wins; 60 polls, 30 seconds apart) and
   requires success, because that check comes from a separate `pull_request_target` workflow that can outlast CI and
   the ruleset requires it next to `gate` (#765). Auto-merge is not enabled on the repository, so the workflow polls
   instead of using `gh pr merge --auto`. Failures leave the PR open.
   After the waits it refreshes the App token (installation tokens expire after one hour), then squash-merges with
   `--match-head-commit` and subject `Docs: regenerate fragment regions [skip ci] (#<PR>)`.

The squash commit's `[skip ci]` prevents push CI and therefore deployment; the regeneration job also guards against
that marker. PR CI never deploys because deployment requires a successful CI run for a push to `main`.
No ruleset bypass or Actions PR-creation setting is needed: the normal PR CI makes the required `gate` eligible.
A ruleset requiring approval still waits for an owner review; the workflow never approves PRs.

The `docs-regen` concurrency group cancels older runs, so bursts reuse one PR. Unchanged regions create no PR and
close a stale docs PR. Main CI validates only fragment schemas while generated regions catch up; PR CI compares
regions with the base or the PR's own fragments. `tests/test_ci_cd_workflow.py` checks the credential scope,
normal PR CI, commit markers and merge ordering; `tests/test_docs_regen_ci.py` exercises failure handling in the
CI and gate-guard wait scripts. End-to-end App operation must be checked after this workflow reaches main.

## Deployment boundary

The daemon itself is deliberately not containerized. It is a host Python process because it coordinates Windows
scheduled tasks, local repository paths, Docker sandbox creation, GPU/model controls, and native provider logins.

The owner explicitly approved the repository-scoped self-hosted runners. The `agent-harness-tower-linux` runner gives
trusted `main` workflow code the tower user's filesystem, Docker, credentials, and service-restart authority. It is
reserved for deployment; tests and SonarCloud use GitHub-hosted Windows, and automated review uses the
`agent-harness-review` pool.

The `sonar` job in `.github/workflows/ci.yml` is the analysis. It runs on GitHub-hosted `windows-latest` against
SonarCloud organization `dflippojr`, project key `dflippojr_agent-harness` (`sonar-project.properties`), host
`https://sonarcloud.io`, using the repository secret `SONARCLOUD_TOKEN`: the Actions store for ordinary PRs and
`main` pushes, and the **Dependabot** store for Dependabot PRs. Fork PRs skip the Sonar job. If a same-repository
Dependabot PR has no token yet, the job emits a notice and a summary explicitly stating that analysis was not
performed, and skips the scanner. Add an authorized analysis token in GitHub Settings → Secrets and variables →
Dependabot → New repository secret, named exactly `SONARCLOUD_TOKEN`, then rerun the Dependabot PR workflow and
verify analysis for its head commit in SonarCloud. A missing token on an ordinary PR or `main` push fails the
Sonar job. The `test` job installs `pytest-cov`
and `pytest-xdist` as extras (not in `requirements.txt`), logs `NUMBER_OF_PROCESSORS` to confirm the hosted 4-vCPU
shape, then runs `python -m pytest tests -q -n 4 --dist loadfile` with pytest-cov Cobertura output
(`coverage.xml`), which `sonar` downloads before scanning. Both jobs use `windows-latest`, so the absolute source paths
in `coverage.xml` resolve. Worker count is fixed at 4 (not pytest's auto count) because a public-repo
`windows-latest` runner has 4 vCPUs. pytest-cov merges xdist workers into one `coverage.xml`.
`COVERAGE_CORE=sysmon` is not used: `.coveragerc` sets `branch = True`, and coverage.py's sysmon core cannot
measure branches on Python 3.12 (this job; sysmon branch support starts at 3.14). The scan still uses
`sonar.qualitygate.wait=true`. The suite is Windows-native; Ubuntu hosted runners fail coverage collection on
`msvcrt`, console-creation flags, and Windows paths. Docker-image and GPU tests still skip on the hosted runner
and do not contribute coverage. Non-Python trees (`ops`, scripts that are not `.py`) are excluded
from the coverage metric. Confirm the SonarCloud project still uses a gate that includes coverage on new code;
the workflow cannot set that condition itself. PR analysis stays on GitHub-hosted Windows; it must not move to
the tower.

Serial `sonar` job baseline from nine successful hosted runs on 2026-09-23 (createdAt 02:47–19:42 UTC): job
wall-clock median 818 s, worst 1026 s; `Run tests with coverage` median 684 s, worst 843 s (the 828 s figure in
issue #191 is run 35911097047). Pushing a branch without a pull request does not trigger `ci.yml`, and the
`changes` skip list that skips `test` skips `sonar` with it (none of those files are Sonar sources).

SonarCloud **Automatic Analysis must stay OFF**. This workflow is the analysis; turning Automatic Analysis on would
duplicate and fight it. To rotate the token, create a new SonarCloud user token, replace the repo Actions secret
`SONARCLOUD_TOKEN` in both the Actions and Dependabot stores, then confirm analysis on an ordinary pull request,
a `main` push, and a real Dependabot PR. A green notice-only job is not evidence of analysis. The old `SONAR_TOKEN` and
`SONAR_HOST_URL` secrets are already deleted and must not be reintroduced.

Local SonarQube on `localhost:9000`, published by `ops/tailscale/serve.ps1` as `https://<tower>.ts.net:9000`, is
local-only. CI does not depend on it.

## Scheduled dependency updates

`.github/dependabot.yml` schedules weekly updates for pip requirements at `/` (`requirements.txt`,
`requirements-repomap.txt`, and `requirements-telemetry.txt`), Dockerfiles in `/sandbox`, `/ops/egress`, and
`/reference/{hermes,openclaw,opencode,openhands}`, Docker Compose at `/ops/observability`, and GitHub Actions at `/`.
Dependabot's Dockerfile discovery includes `sandbox/cli.Dockerfile`; its Compose filename pattern includes
`ops/observability/docker-compose.tempo.yml` ([Docker fetcher](https://github.com/dependabot/dependabot-core/blob/main/docker/lib/dependabot/docker/file_fetcher.rb),
[Compose fetcher](https://github.com/dependabot/dependabot-core/blob/main/docker/lib/dependabot/docker_compose/file_fetcher.rb)).
`ops/egress/compose.yaml` uses only a locally built image, so external image updates come from its Dockerfile.
After this configuration reaches `main`, verify update jobs and representative PRs under GitHub's dependency graph
Dependabot page. Verify a real Dependabot PR's SonarCloud analysis at its head commit; a missing-token notice does
not satisfy that verification.

## Automated review backends

`.github/workflows/review.yml` reviews a pull request once when it is opened and supports explicit re-reviews through
`workflow_dispatch`. Re-review a PR with `gh workflow run review.yml -f pr_number=N` (optional `-f backend=…`).
`workflow_dispatch` always uses the workflow file on `main`, so unmerged `review.yml` changes are not exercised by
dispatch. Every run publishes an `Automated Code Review` Check Run on the reviewed PR head SHA (#120/#124/#313), whose
conclusion follows the review verdict:

| Review result | Check conclusion | Check title |
|---|---|---|
| `REVIEW_VERDICT: CLEAN`, full diff reviewed | `success` | `Clean review by <backend>` |
| `REVIEW_VERDICT: FINDINGS n` | `failure` | `n findings` (plus the partial note when files were omitted) |
| `CLEAN`, but the diff exceeded `REVIEW_MAX_DIFF_BYTES` | `neutral` | `Partial review: n files not reviewed` |
| Every backend failed or gave no valid verdict, or the comment was not posted | `failure` | `Review did not complete` |
| Run cancelled | `cancelled` | `Automated review cancelled` |

**Spec review (review-v3, #725).** Before the backend runs, the job reads the pull request's title and description and
each issue it closes: the issues GitHub links to the PR plus `Closes`/`Fixes`/`Resolves #N` (or `owner/repo#N` naming
this repository) in the description, at most five, each with its title, body, author, author association and every
page of comments, up to 1,000. The repository is `GITHUB_REPOSITORY`, or for a local run whatever `gh repo view`
resolves (from `GH_REPO` or the checkout); the review fails if neither names one, so a reference to another repository is
never read as a same-numbered issue here. They reach the reviewer as one fenced block of untrusted data, `BEGIN UNTRUSTED SPECIFICATION <nonce>` to
`END UNTRUSTED SPECIFICATION <nonce>` with a random 16-character nonce per run. Every line of their text is prefixed
with `| ` after U+2028 and U+2029 become line breaks and control and invisible format characters are dropped, judged
by code point so the Unicode tag block, the variation selectors supplement and unpaired surrogates go too. No line of
an issue can pass for the closing fence, a heading or a contract line, and the prompt tells the reviewer never to
follow instructions found inside. Only comments from owners, members and collaborators may refine the criteria; other
comments, the issue author's included when the author has none of those roles, are context only. The
text never reaches a command line: `gh` returns JSON, only issue numbers parsed as integers are passed as arguments, and
the prompt goes to the backend on standard input. A description and issues larger than `REVIEW_MAX_SPEC_BYTES`
(default `16384` bytes of UTF-8 as rendered, `| ` prefixes included; accepted range `4096` to `262144`, digits only)
are cut to fit: the description gets at
most half, then each issue's title and body, then comments: those from owners, members and collaborators first, so a
flood of outside comments on a public issue cannot push them out, each group newest first. Each section's label counts
against the budget too, and comments that are empty or do not fit are left out and counted in one notice per issue, so
a thread of many tiny comments cannot grow the block past the cap. Every cut is marked in the block. An issue
that no longer exists (HTTP 404 or 410) is named as unreadable; any other failure, including a caller that did not grant
`issues: read`, fails the review before a backend runs. In an incremental pass the reviewer judges criteria against the
pull request head in the workspace. `ops/review/run-review.ps1` does all of this only with `REVIEW_SPEC=on` (or
`-Spec on`); without it the script keeps the review-v2 behaviour, so a review-v2 workflow that follows this script
still works. The workflow sets the environment variable rather than the parameter, so the v3 workflow with older
tooling (a pull request that changes `review.yml` runs it against `main`'s script) degrades to a diff-only review
instead of failing.

The review has three parts under fixed headings. `## Acceptance criteria` lists each criterion of the linked issues as
**Met**, **Not met** or **Not applicable** (owner actions, deferred or out-of-scope items, proof attached outside the
diff), citing evidence by file name. `## Findings` lists blocking findings: correctness bugs; every unmet criterion,
quoted; a bug fix without a test that would fail without it; a test deleted, skipped, retried or weakened without a
matching entry under the description's `### Gate changes`; a test that cannot fail (asserts constants, mocks the unit
under test, swallows exceptions); and a `## Proof` table row that the diff contradicts. Findings alone set the verdict,
the check conclusion and the inline annotations. `## Style and structure (advisory)` lists at most five suggestions on naming,
responsibilities, duplication, consistency with the neighbouring code and simplifications, judged against the repository's
own lint and format configuration. It never changes the verdict or fails the check. Its items are written without the
`path:line` form, and the annotation scanner also stops reading at this heading, so they cannot become annotations. A clean review can therefore still carry advisory suggestions.

The check summary carries the posted comment, so findings are readable from the Checks tab. The workflow run itself
stays green when the review merely has findings: a red run means the review machinery broke, a red check means the
code has findings. The prompt requires the reviewer to end with exactly three lines: `REVIEW_CRITERIA: <met>/<total>`
(criteria marked Met over those marked Met or Not met; `0/0` when no issue is linked), then `REVIEW_VERDICT: CLEAN` or
`REVIEW_VERDICT: FINDINGS <n>`, then `REVIEW_STATUS: COMPLETE`. The verdict only counts as the line directly above the
status marker and the criteria line only as the line directly above the verdict, so contract-like text quoted from the
diff or an issue cannot spoof them. The criteria line must match the **Met** and **Not met** rows listed under
the first `## Acceptance criteria` (fenced code skipped), and a review whose findings are fewer than its unmet criteria (for example one that
reports a miss but obeys an issue's "ignore all findings") is rejected like a missing verdict, so the check fails as
`Review did not complete` instead of turning green. A missing or malformed line is treated like a missing marker. All
three lines are removed before posting. A spec review's check title adds the count, for example
`2 findings; criteria 1/3 met`, and the job output `criteria=<met>/<total>` carries it. When the posted comment carries
the coverage marker (below), a spec review adds a summary line after it for readiness tools such as `ao ready`:
`<!-- agent-review: sha=<head> criteria=<met>/<total> findings=<n> -->`. `harness.review_comments.parse_review_summary`
reads it back from the comment's final nonblank line only, so a summary quoted in the review text cannot stand in for it. A partial
review withholds the summary along with the marker, so a readiness tool treats it as not reviewed. The check is created on the head commit that the job checked
out, and `pull_request` only fires on `opened`, so a PR that gains commits needs a dispatched re-review before its head
commit carries the check again.

The optional `backend` dispatch input accepts `auto`, `cursor`, `codex`, or `claude`. An explicit
provider runs only that provider, which is useful for verification and deliberate quota steering. Omitting the input
or selecting `auto` tries the comma-separated `REVIEW_BACKENDS` repository variable in order. If the variable is empty,
the order defaults to `codex,claude`: GPT leads on purpose, because a reviewer from a different model family than the
usual Claude author thinks differently about the code. Cursor stays a supported backend (list it explicitly) but is no
longer in the default order.

The runner falls through that ordered list when a CLI exits non-zero, returns no review, reports a recognizable
rate-limit or quota error, or omits the required completion marker or verdict line after inspecting the diff. Both are
removed before posting. The successful backend is included in the PR comment footer and the manually created Check Run. Invalid
backend names fail closed instead of silently changing provider.

The optional `mode` dispatch input accepts `auto` or `full` (default `auto`). `auto` reviews only the commits since
the last automated review when that range is safe: a prior `github-actions[bot]` comment contains an HTML marker
`<!-- agent-review: sha=<40-char head sha> mode=<full|incremental> base=<target branch> -->`, the GitHub compare API
shows that SHA is an ancestor of the current PR head, the range is non-empty, it contains no merge commits, and the
PR still targets the same branch recorded on that marker. Every other case — no marker, a truncated review that
omitted files from the prompt, a missing target branch on the marker, a retargeted PR, `mode=full`, compare
failure, rebase/force-push, a merge of the base branch, or an identical SHA — falls back to `gh pr diff` for a full
pass. Invalid `mode` values fail closed. Posted comments start with a coverage line (`Reviewed the full diff` or
`Reviewed <7-char>..<7-char> (incremental; N commits, M lines)`) and, when the prompt included every file in the
diff, end with the marker for the SHA, mode, and target branch actually used. Truncated reviews still post findings
but omit that reusable marker so a later auto pass cannot treat omitted files as already reviewed.

The external orchestrator must dispatch the pre-merge review with `mode=full` so the merge recommendation is always a
full-diff pass. Intermediate re-reviews after a push can omit the input or pass `mode=auto`.

Set `REVIEW_BACKENDS` under repository **Settings > Secrets and variables > Actions > Variables**. For example,
`claude,codex,cursor` spends Claude quota first while retaining two fallbacks; changing the variable does not require a
workflow edit.

Resolution order is: an explicit `backend` input, then `REVIEW_BACKENDS` for `auto`, then the runner's
`codex,claude` default. For each selected backend, its `REVIEW_MODEL_*` and `REVIEW_EFFORT_*` Actions variables
override the workflow defaults. The runner validates those resolved environment values and passes them to the CLI;
it does not replace them with another model or effort. Invoking the script directly without those environment
values keeps the CLI's own model and effort defaults. A failed backend falls through to the next configured backend.

For agent-harness PR #541, the earlier Sonnet review used these overrides. A read-only check on 2026-10-09 found
all three variables below absent, so they no longer force the earlier choices. This PR does not change Actions
variables; the owner controls them separately, and later changes take precedence over these defaults.
The unset effects in the table describe this branch's workflow once it reaches `main`.

| Variable | Earlier override and effect | Verified state on 2026-10-09 |
| --- | --- | --- |
| `REVIEW_BACKENDS` | `claude`: runs only Claude, with no Codex attempt or fallback | Unset: `auto` uses `codex,claude` |
| `REVIEW_MODEL_CLAUDE` | `claude-sonnet-5`: pins Sonnet when Claude runs | Unset: workflow supplies `claude-opus-5-5` |
| `REVIEW_MODEL_CODEX` | `gpt-6-luna`: pins Luna if Codex is selected | Unset: workflow supplies `gpt-6.1-sol` |

Both effort variables were also absent at that check: the workflow supplies Codex `xhigh` and Claude `high`.
These are this branch's workflow defaults; a dispatch before merge uses `main`'s workflow and review tooling.
At that check, `main` still tried `codex,claude,cursor` and supplied no model or effort defaults, leaving those
choices to the CLIs when the variables were unset.

Optional `REVIEW_MODEL_CLAUDE`, `REVIEW_MODEL_CURSOR`, and `REVIEW_MODEL_CODEX` pin the model each backend CLI is asked
to use (`--model` on `claude`, Cursor `agent`, and `codex exec`). When a variable is set, the runner passes that flag and
the PR comment footer plus `model=` job output name it, for example `Automated review backend: **claude (claude-opus-5-5)**.`.
When a variable is unset, the workflow supplies a default model: `gpt-6.1-sol` for Codex and `claude-opus-5-5` for Claude
(Cursor has none: it keeps the CLI default and the footer names only the backend). Values must match
`[A-Za-z0-9][A-Za-z0-9._:+/\-]*`; anything else (spaces, quotes, leading dashes, shell metacharacters) fails closed
before a backend runs.

Optional `REVIEW_EFFORT_CLAUDE` and `REVIEW_EFFORT_CODEX` pin reasoning effort: `--effort <value>` on `claude`
(`low`, `medium`, `high`, `xhigh`, `max`) and `-c model_reasoning_effort="<value>"` on `codex exec` (`low`, `medium`,
`high`, `xhigh`, `max`). When set, the footer and a new `effort=` job output include it, for example
`Automated review backend: **claude (claude-opus-5-5, high)**.`. Unset falls back to the workflow defaults, `xhigh` for
Codex and `high` for Claude; a whitespace-only value keeps the CLI default and the footer unchanged. Any other value
fails closed before a backend runs, and all effort variables are validated up front even for backends not used. Cursor
has no effort variable: choose effort through the model id (for example `cursor-grok-4.6-medium` in
`REVIEW_MODEL_CURSOR`).

Optional `REVIEW_MAX_DIFF_BYTES` sets how many bytes of PR diff are embedded in the review prompt (default `204800`; accepted range `20480` to `2097152`, digits only). A larger cap covers more of a big PR but grows the prompt, so each review costs more tokens and risks exceeding the model's context; a smaller cap is cheaper but omits more. An invalid value fails closed before a backend runs. When the diff exceeds the cap, whole files are dropped by a fixed rule: source files are kept first, then tests, then docs, then lockfiles and generated or vendored output, in diff order within each tier; a file that does not fit is omitted even if a smaller later file still fits. The comment then opens with `PARTIAL REVIEW: reviewed N of M files (X of Y KB of diff). Not reviewed: <files>` instead of `Reviewed the full diff`, and the `<!-- agent-review: ... -->` marker is withheld so the next run reviews the whole PR instead of treating it as covered. A single file larger than the cap is always listed as not reviewed. Omitted files are not reviewed in additional passes; raise the cap to cover them.

All three CLIs run under the review runner service user and must be logged in for that same user. The workflow runs
every step in PowerShell 7 (`shell: pwsh`, review-v4, #806), so a review runner needs PowerShell 7.5 or later (`pwsh`;
earlier 7.x releases turn GitHub's ISO 8601 timestamps into dates), `git` and `gh` on the service user's PATH on Linux
and Windows alike. The backends receive only an allowlisted environment: the Windows
profile and system variables, and on Linux `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `LANG`/`LC_*` and the XDG base
directories, so each CLI finds the service user's login. Cursor uses ask mode
with its sandbox enabled on non-Windows hosts; the flag is skipped on Windows because that CLI does not support it.
Windows ask mode is not an OS sandbox; keep Cursor opt-in rather than adding it to the default Windows pool. The
wrapper starts Cursor's bundled `node` from the newest version under `~/.local/share/cursor-agent/versions` (or
`$XDG_DATA_HOME/cursor-agent`) on Linux and `%LOCALAPPDATA%\cursor-agent\versions` (`node.exe`) on Windows. Codex
ignores the service user's configuration, restores only the required unelevated Windows sandbox setting on Windows (on
Linux, Codex uses its own OS sandbox and gets no Windows setting),
disables apps and plugins, and supplies an empty MCP server table before entering its read-only sandbox.
Claude exposes only Read/Grep/Glob, with `--allowedTools Read(./**)` anchored to the PR workspace as its working
directory. No bare Read, Grep or Glob allow is passed, and `--setting-sources=` disables inherited settings that could
add broader permissions or additional directories. `--restricted` explicitly fences built-in file tools to working
directories, including otherwise implicitly permitted profile-side transcripts and memory; `--safe-mode` disables
customizations and automatic memory, and `--no-session-persistence` avoids saving this review as a runner session.
The CLI must support these flags (restricted mode requires v2.1.248 or later); an unsupported flag fails closed.
See [Claude permission rules](https://code.claude.com/docs/en/permissions) and the [CLI flag reference](https://code.claude.com/docs/en/cli-reference).
The wrapper, rather than a model, writes the final comment file. Before writing `review-output.md`, it scans the
public review body using the diagnostic redaction patterns (bearer values, named credentials, provider tokens and
long tokens) plus two path rules. A match discards the whole body, removes any stale output and fails closed with
`Review did not complete`; nothing is posted or copied into the check summary. The path rules never parse or
canonicalize a path, so dot segments, quoting, whitespace and punctuation cannot hide one:

- Any Windows absolute root is rejected, whatever follows it: a drive root (`C:\` or `C:/`), a UNC or extended
  prefix (`\\server\`, `//server/`, `\\?\`, `\\.\`), or a `file:` URL.
- Any separator followed by a profile directory name (`Users`, `home`, `root` or `Documents and Settings`, including
  Windows trailing-period, trailing-space and short-name spellings) is rejected unless it is part of a known
  repository path. `docs/users.md` and `root-ca.md` are not profile directories; an unknown `examples/home/x.py` is
  rejected even though it is relative.

The rules run on the raw body and on what a reader would see: percent escapes, HTML character references as an HTML5
parser reads them (numeric references and legacy names such as `&shy` need no semicolon, as in a raw HTML block;
HTML5 names for ASCII punctuation such as `&sol;` and `&lsqb;`, which .NET does not decode, are decoded too; any other
named entity with a semicolon left undecoded, such as `&fjlig;` or `&ZeroWidthSpace;`, fails closed as rule
`unknown-entity`), characters a reader may not see (controls other than whitespace, noncharacters and Unicode's
default-ignorable code points, such as format characters, the soft hyphen and Hangul fillers), Unicode compatibility forms and Markdown backslash escapes are decoded first. Three
approximate renderings then read markup from the raw text, so an escaped or entity-encoded bracket stays text:
`rendered` drops the raw HTML that CommonMark passes through (tags, comments, processing instructions, declarations,
CDATA) and the link and image targets GitHub hides, `literal` keeps every link target in case the text was not a link,
and `html` reads each line as GitHub reads a raw HTML block (an HTML5 tokenizer, so a quoted attribute or a comment may
hold `>`). Both Markdown renderings drop emphasis, code and strikethrough markers, and every bracket when the text may
define a link reference, which is any `]:` so definitions in block quotes and lists count (otherwise brackets render
literally, so `test_x[case-1]` stays two words). What the renderings cannot settle fails closed, outside fenced code
blocks at column 0: rule `bidi-control` rejects any bidirectional formatting character, raw or decoded (it reorders what a
reader sees, even in code); rule `raw-html` rejects markup that CommonMark reads across lines (or an unterminated tag or
comment in a possible HTML block), markup that CommonMark and an HTML5 tokenizer end at different places (a comment
holding `--!>`, an instruction or CDATA holding `>`), markup holding a backtick, a `dir` attribute, and the elements
whose text GitHub keeps out of place (`rp`, `rt`, `rb`, `rtc`, `ruby`, `table`, `video`, `bdo`, `bdi`), even when only
named in inline code; rule `link-target` rejects a link whose target holds a backtick or whose text holds an unpaired
one; rule `link-depth` rejects links and images nested more than 8 deep, which the `rendered` form stops unwrapping
(even in fenced code). Math and Mermaid are checked in the raw, `decoded` and `unescaped` forms, so a reference or escape
cannot hide them: rule `fence-info` rejects a fence line (indented, in a list item or block quote, too) whose info string
is not a single plain language token (letters, digits, `_+#.-`), since GitHub decodes references and escapes there; rule
`math` rejects a `math` fence (any case), `$$`, and a `$...$` pair GitHub may draw as math (the opening `$` after no
ASCII letter or digit and before a non-space, the closing `$` after a non-space and before no ASCII letter or digit,
within a paragraph; `&#36;` and `&dollar;` count and `\$` does not protect). A code span hides a `$` only when every
backtick run outside fenced code closes on its own line and no backtick sits inside `<...>` or beside an escaped
backtick (otherwise no span hides anything), and never on a line where a span holds a `|`, since a table row is possible
wherever a `|` is. Rule `line-break` rejects, raw or decoded and before any of this, every line break but LF and CRLF:
a bare CR (GitHub ends a line there; the scans do not), vertical tab, form feed, NEL, U+2028 and U+2029. So
`$5 and $10`, `$HOME/$USER` and `` `$a` `` still publish. Rule `mermaid` rejects a `mermaid` fence. GeoJSON, TopoJSON
and STL are not modeled, and no scan stops a value spelled out in pieces in prose. Once every rule above passes, the
wrapper also asks GitHub's own renderer: it sends the body to `POST /markdown` (`gfm` mode, the repository as context,
through `gh` on stdin), drops every tag from the returned HTML, decodes its character references and runs the
credential, path and long-token rules and `bidi-control` on that text (forms `github`, `github-decoded` and
`github-unescaped`), so the scan also sees what GitHub's renderer shows. It is an extra form, never a replacement: the
HTML does not show what the browser does next (hidden ruby text, `dir` reordering, math and Mermaid drawn by scripts),
so every rule above stays. A body those rules reject is never sent, and validated metadata is never rendered. If the
call fails, returns nothing or takes more than 30 seconds, rule `markdown-render` fails the body closed; the warning says
which, without the body or `gh`'s output. `tests/fixtures/review_markdown/rendered.json` holds GitHub's real answers for
the scan's test payloads (regenerate it with `Update-RenderedFixtures.ps1` beside it), so the tests make no network
calls; it also records which fail-closed rejections GitHub renders harmlessly, the candidates for relaxing a rule in a
separate, reviewed change. Known
citations are masked in the raw text before decoding and again in each decoded form, in their raw, normalized and
rendered spellings; a name counts as known only if every one of those spellings is relative. This is intentionally
conservative: benign prose and examples matching a credential or path rule are also rejected, so the review prompt
tells the backend to cite only repository-relative paths and describe credentials in words. A run of 40 or more token characters is not
treated as a token when it reads as an identifier (only letters, slashes, underscores and hyphens, with at least one
underscore or hyphen, and digits only as up to four ending a lowercase word, as in `c1` or `sha256`), so long test and
function names stay publishable. A rejection logs a warning per hit naming the rule, the scan form (`raw`, `decoded`,
`unescaped`, `rendered`, `literal`, `html`, `github`, `github-decoded` or `github-unescaped`) and the offset and length
of the match, never the matched text. The base ref in the coverage
marker is already validated as a branch name and is checked against the credential patterns only. For publication, the bearer
and named-credential rules (`token`, `secret`, `password`, `api_key` and similar followed by `:` or `=`) match only a
value that looks like a credential: a non-empty quoted literal that is not a reference (`$VAR`, `${{ ... }}`, `%VAR%`,
`{x}`, `<x>`), or a token-like value (6 or more characters including a digit, or 20 or more token characters). Prose
such as "a Bearer token" and quoted code such as `token: str`, `self.token = token` or
`password: ${{ secrets.X }}` are publishable. Job-log diagnostics keep the broader original patterns. Git-quoted names
that are malformed or not valid UTF-8 never block a review; they simply do not match a citation.

Known repository paths come from the Git index and every diff file, including deletions and both rename/copy sides.
Git-quoted paths are decoded as UTF-8 bytes, and patch and rename/copy metadata disambiguate filenames containing the
diff header's ` b/` separator. Exact citations of known paths (including `./` prefixes and Windows separators) are
masked for the profile-directory and long-token rules only; a known path never exempts an absolute root, a profile
prefix in front of it, or a credential pattern. For the long-token rule, a web link's path is split at each literal `/`
(before decoding, so `%2F` never splits), and a long documentation URL is not one token; its query and fragment stay whole, so a signature there is still caught. Validated Git metadata in the coverage marker is added only after the scan.
Stderr shown in job-log warnings keeps the message before a path and replaces the rest of that line with
`[REDACTED PATH]`.

The wrapper fetches the pull request diff before starting a backend and embeds up to 200 KB of complete file patches directly in the prompt, so review
sandboxes do not need GitHub network access. Larger diffs identify every omitted file in the prompt. After installing or
changing a CLI, verify each backend explicitly against a disposable pull request:

```powershell
gh workflow run review.yml -f pr_number=N -f backend=cursor
gh workflow run review.yml -f pr_number=N -f backend=codex
gh workflow run review.yml -f pr_number=N -f backend=claude
gh workflow run review.yml -f pr_number=N -f mode=full
```

Inspect each run and its PR comment. Record any CLI that is not runnable under the runner service user plainly in the
pull request verification notes; an interactive desktop login is not evidence that the runner service account is
authenticated.

`workflow_run` executes the workflow file from the default branch and can access secrets, so its jobs reject every
event except a successful `CI` run caused by a push whose head branch is `main`. They check out and deploy only the
reported `head_sha`; they never check out or execute pull-request code. Third-party actions are pinned to exact
commits, main deployments serialize rather than being canceled midway, and the GitHub `tower-production` environment
accepts deployments from `main` only.

### Adopting the review workflow

Other projects call this repository's `review.yml` as a reusable workflow (`on: workflow_call`) instead of carrying a
copy, so a fix here reaches every project. The job checks out `ops/review/run-review.ps1` from
`dflippojr/agent-harness` at the `tooling_ref` input (default `main`, the trusted default branch) into `.review-tooling`; the consumer repo
needs no `ops/review` directory. This repository also checks out tooling from the trusted default branch, never
from the triggering commit; the PR head is checked out separately in `pr` for review context only.

**Caller file.** Add `.github/workflows/review.yml` to the consumer repo, with that repo's runner label in `runs_on`:

```yaml
name: Automated Code Review

# Dispatch only, never pull_request: the reviewer reads untrusted PR content with shell access on the self-hosted
# runner, and a fork's pull_request run would execute the fork's copy of this file. The shared job also refuses
# pull requests from forks.
on:
  workflow_dispatch:
    inputs:
      pr_number:
        description: "PR number to review"
        required: true
      backend:
        description: "Review backend (auto uses REVIEW_BACKENDS)"
        required: false
        default: auto
        type: choice
        options: [auto, cursor, codex, claude]
      mode:
        description: "Review coverage (auto = incremental when safe)"
        required: false
        default: auto
        type: choice
        options: [auto, full]

permissions:
  contents: read
  pull-requests: write
  checks: write
  issues: read

jobs:
  review:
    uses: dflippojr/agent-harness/.github/workflows/review.yml@review-v4
    with:
      pr_number: ${{ inputs.pr_number }}
      backend: ${{ inputs.backend }}
      mode: ${{ inputs.mode }}
      runs_on: '["self-hosted","Linux","X64","financial-planner-review"]'
      tooling_ref: review-v4
    secrets: inherit
```

Do not add a workflow-level `concurrency` group to the caller; the shared job already serializes per repository and
PR. Optional inputs: `max_diff_bytes` (otherwise the consumer's `REVIEW_MAX_DIFF_BYTES` variable, then `204800`),
`max_spec_bytes` (review-v3; otherwise the consumer's `REVIEW_MAX_SPEC_BYTES` variable, then `16384`) and
`tooling_ref` (use the trusted default branch or a maintainer-pinned tag, never a PR ref). The `REVIEW_*` repository variables are read from the consumer
repository.

**Runner label.** Register a self-hosted Linux runner for the consumer repo with
`sudo ops/github/install-runner.sh --token <token> --repo dflippojr/<project> --labels <project>-review --name <name>`
under a service user whose Codex, Claude, and Cursor CLIs are logged in, and pass that label in `runs_on`
(see [Review runner pool](#review-runner-pool)). For a Windows fallback runner, use
`ops/github/install-runner.ps1 -Labels <project>-review` and `"Windows"` in place of `"Linux"` in `runs_on`; review-v4
runs on either, as long as the runner has PowerShell 7 (`pwsh`).

**Runner access (owner step).** Create a self-hosted runner group in GitHub and restrict it to the specific trusted
review/deploy workflows that need those runners, with workflow refs on the default branch or trusted tags. Labels
select runners but do not restrict who may schedule them. Keep fork-PR approval required for all outside contributors.
This GitHub administration step cannot be enforced by this repository change; the owner must configure it.

**Permissions.** The caller's `permissions` block must grant `contents: read`, `pull-requests: write` (PR comment),
`checks: write` (the `Automated Code Review` check) and, from review-v3, `issues: read` (the linked issues). A called
workflow can only narrow these, so a review-v3 caller without `issues: read` fails its reviews as
`Review did not complete`.

**Actions allowlist.** A consumer whose Actions policy allows only selected actions (for example GitHub-owned plus
`SonarSource/sonarqube-scan-action@*`) must also allow `dflippojr/agent-harness/.github/workflows/review.yml@*`
(Settings -> Actions -> General -> "Allow or block specified actions and reusable workflows").

**Version pin.** The `@review-v4` reference selects the workflow definition. The script follows trusted `main` by
default; pinning only the workflow does not stage script changes. For staged rollout, set `tooling_ref: review-v4`
as in the caller above so both workflow and script follow the same maintainer-controlled tag. After a change has
been verified here, the owner moves that tag deliberately:

```powershell
git tag -f review-v4 <verified commit on main>
git push -f origin refs/tags/review-v4
```

A breaking change to the caller contract (renamed or new required inputs, or a new permission) gets a new tag.
review-v3 added the spec review and the `issues: read` permission, so it is a new tag: the review-v2 tag stays on its
commit and its callers keep the diff-only review until each repository moves its caller (workflow ref, `tooling_ref`
and the `issues: read` grant) to review-v3. review-v3 exists: the owner created it at the merge commit of #725 on `main`,
after a dispatched review here had passed.

review-v4 (#806) runs the same spec review on Linux or Windows runners: every step uses PowerShell 7 (`shell: pwsh`)
instead of Windows PowerShell 5.1, which Linux does not have, and the default label for this repository's own runs is
`["self-hosted","Linux","X64","agent-harness-review"]`. The caller contract is unchanged except for the OS label in
`runs_on`, but review-v3's steps cannot start on a Linux runner, so review-v4 is a new tag and review-v3 stays on its
commit for Windows-only callers. The owner creates review-v4 at the merge commit of #806 on `main` after a dispatched
review has passed on a Linux runner; each caller then moves its `uses` ref, `tooling_ref` and `runs_on` OS label in one
change. Callers pin it as shown above and need no tag of their own.

**Gate guard required check (owner step).** The `main` ruleset must require `gate-guard-target / gate-guard` next to `gate`.
That check is the job `gate-guard-target` in `.github/workflows/gate-guard-target.yml`, which runs from `main`, so it appears
on pull requests only after that file is merged, and it stays advisory until the ruleset names it. A ruleset that still
names the old `gate-guard` check must be changed, because that check is no longer posted. Without this step a pull request can still pass
`gate` after deleting tests, because `gate` alone judges nothing.

**Required check on pull requests.** The review does not run on pushes to `main`; it is a required check for PRs into
`main`, so a PR cannot merge without a clean (or partial, `neutral`) review of its head commit. A runner outage then
blocks merging until a re-run or an admin bypass. Apply it per repository with a branch ruleset (`15368` is the GitHub
Actions app, which owns checks created with `GITHUB_TOKEN`):

```powershell
@'
{
  "name": "Require automated code review",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [ { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" } ],
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [ { "context": "Automated Code Review", "integration_id": 15368 } ]
      }
    }
  ]
}
'@ | Set-Content -Encoding ascii ruleset.json
gh api --method POST repos/dflippojr/<repo>/rulesets --input ruleset.json
```

The `bypass_actors` entry (repository role 5, admin) keeps the admin bypass for outages and for direct pushes to `main`,
which a required status check otherwise blocks.

**Migrating a copy.** For a repository that carries a copied `review.yml` and `ops/review/run-review.ps1` (as
`financial-planner` does): add the allowlist entry, replace its `review.yml` with the caller above (its runner label is
`financial-planner-review`), delete `ops/review/`, and dispatch a review against a disposable PR to confirm the comment,
the check conclusion, and the check summary.

## Retired CI runner pool

Until 2026-10, pytest also ran on three self-hosted `agent-harness-ci` runners on the tower
(`dflippotower-agent-harness-ci`, `-ci-2`, `-ci-3`, installed under `D:\Agents\github-runner-ci*` with logon tasks
`AgentHarness-GitHubRunner-CI*`), duplicating the hosted suite that SonarCloud needs anyway. Each job ran
`pytest -n 8` next to llama-server on a 31.8 GB machine, and PR runs waited on the busy tower (7–51 minutes, versus
9–14 for the hosted job). Tests now run once, GitHub-hosted, in `ci.yml` `test`.

After that change is on `main`, remove each member: delete the runner in GitHub (**Settings > Actions > Runners**),
then

```powershell
Unregister-ScheduledTask -TaskName AgentHarness-GitHubRunner-CI -Confirm:$false
Remove-Item -LiteralPath D:\Agents\github-runner-ci -Recurse -Force
```

and repeat with `-2` and `-3`. Parallel-safety still holds for the hosted `-n 4 --dist loadfile` run: Docker test
networks are unique per process, listen ports use `port=0`, and data dirs stay under `tmp_path`. Local serial escape
hatch: `python -m pytest tests -q -p no:xdist`.

## Tower runner

The tower runs Ubuntu since 2026-10-26. Production runs there from the live checkout
`/home/daniel/Projects/agent-harness` as systemd user units of `daniel` (`~/.config/systemd/user/`, lingering on):
`agent-harness-main-daemon.service`, whose `ExecStart` runs `~/.local/share/agent-harness/bin/run-daemon.sh`,
`agent-harness-main-llama.service` and `agent-harness-hub.service`. `harness-metrics-proxy.service` is a system unit,
and the deployment leaves it alone. The Windows runner
`dflippotower-agent-harness` (label `agent-harness-tower`) is retired; `deploy-tower` runs on a Linux runner
registered only to `dflippojr/agent-harness` with the custom label `agent-harness-tower-linux` (#812). The runner
runs as `daniel`, the user that owns the daemon's unit: `systemctl --user` reaches only its own user's manager. That
user must own the live checkout and its parent directory (deployments stage environments and Stables copies next to
it), write `~/.local/share/agent-harness/bin/run-daemon.sh`, use Docker, and have `pwsh` 7 on its `PATH`. No sudo or
sudoers rule is involved.

Register it with a fresh short-lived token (`gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token
--jq .token`) and the official runner's `./config.sh --url https://github.com/dflippojr/agent-harness --token <token>
--labels agent-harness-tower-linux`. The token is never saved. Then keep it running as a systemd user unit of
`daniel` beside the production units, for example `~/.config/systemd/user/github-runner-agent-harness.service`:

```
[Unit]
Description=GitHub Actions runner for dflippojr/agent-harness (agent-harness-tower-linux)
After=network-online.target

[Service]
ExecStart=<runner dir>/run.sh
WorkingDirectory=<runner dir>
KillMode=process
KillSignal=SIGTERM
TimeoutStopSec=5min

[Install]
WantedBy=default.target
```

and `systemctl --user daemon-reload`, `systemctl --user enable --now github-runner-agent-harness.service`. The
deployment also works from a runner started another way as `daniel`, since it sets `XDG_RUNTIME_DIR` itself. The
`tower-production` environment keeps its protection.

Before the first real deployment, dry-run the script on the tower as `daniel`, against the real paths. It changes
nothing and never stops or starts the unit; it reads the unit with `systemctl --user show`:

```
git clone --depth 1 https://github.com/dflippojr/agent-harness /tmp/ah-release
gh repo clone dflippojr/stables /tmp/ah-stables -- --depth 1 --branch "$(jq -r .tag /tmp/ah-release/tools/stables/pin.json)"
pwsh -NoProfile -File /tmp/ah-release/ops/harness/deploy-ci.ps1 -DryRun \
    -Commit "$(git -C /tmp/ah-release rev-parse HEAD)" -ReleaseRoot /tmp/ah-release -StablesRelease /tmp/ah-stables
```

It prints `[deploy] agent-harness-main-daemon.service runs /home/daniel/.local/share/agent-harness/bin/run-daemon.sh`,
then each step it would take (`[dry run] systemctl --user stop ...`, the venv and pip commands, the pointer writes,
the fast-forward, the image tags), and fails, before any of them, on the same guards a real run checks: a missing user
unit, a unit that runs another supervisor or checkout, a dirty or non-`main` live checkout.

The live checkout must itself be on `main`; deployment intentionally fails closed on another branch or detached HEAD.
After first confirming that the checkout has no work that must be preserved, put it on `main` and fast-forward it:

```
cd /home/daniel/Projects/agent-harness
git status --short
git switch main
git fetch origin
git merge --ff-only origin/main
```

Do this as an explicit maintenance action, not from the Actions runner. Never discard local changes to make a deploy
pass.

## Review runner pool

Automated review (`.github/workflows/review.yml`) uses repository-scoped self-hosted runners that share the
`agent-harness-review` label. Each member takes one job, so reviews of different PRs can run in parallel.

### Linux (the tower, from 2026-10-26)

The tower runs Ubuntu, so the pool runs there as systemd services under a dedicated service user. Install each member
with `ops/github/install-runner.sh` (the Linux counterpart of `install-runner.ps1`), as root, one at a time, with a
fresh short-lived registration token for each. The owner or liaison does this step: the token needs the owner's `gh`.

```bash
for n in 1 2 3; do
  token=$(gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token)
  sudo ops/github/install-runner.sh --token "$token" --labels agent-harness-review \
    --name "dflippotower-agent-harness-review-$n"
done
```

Each member unpacks the pinned runner release (SHA-256 checked) into
`/home/github-runner/runners/<name>`, registers it as the `github-runner` service user (created if missing, with no
sudo or docker group), and installs the runner's own systemd unit (`svc.sh install github-runner`), so it starts at
boot and jobs never run as root. `--user` and `--install-dir` choose another user or directory; the directory must stay
under `/home`, because the publication scan rejects home-directory paths and so cannot post a leaked runner path. A
consumer repository's runner adds `--repo dflippojr/<project> --labels <project>-review`. `--dry-run` prints the plan
and changes nothing. Install `pwsh`, `git` and `gh` for the service user, and log in its Codex, Claude and Cursor CLIs
as that user (`sudo -iu github-runner`), before dispatching a review.

To repair or remove a member, delete the runner in GitHub (**Settings > Actions > Runners**), then from its install
directory run `sudo ./svc.sh stop`, `sudo ./svc.sh uninstall`, and delete the directory.

### Windows (fallback boot)

The Windows members remain as a fallback for a Windows boot of the tower. review-v4 needs PowerShell 7 (`pwsh`) on
them too. They are named, installed, and started as:

| GitHub name | Install dir | Hidden logon task |
| --- | --- | --- |
| `dflippotower-agent-harness-review-1` | `D:\Agents\github-runner-review-1` | `AgentHarness-GitHubRunner-Review-1` |
| `dflippotower-agent-harness-review-2` | `D:\Agents\github-runner-review-2` | `AgentHarness-GitHubRunner-Review-2` |
| `dflippotower-agent-harness-review-3` | `D:\Agents\github-runner-review-3` | `AgentHarness-GitHubRunner-Review-3` |

Use the existing parameterized installer (`ops/github/install-runner.ps1`). Do not add a pool wrapper. Obtain a fresh
short-lived registration token, then install one member at a time with distinct `-InstallDir` / `-Name` / `-TaskName`
and `-Labels agent-harness-review`:

```powershell
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token `
  -InstallDir D:\Agents\github-runner-review-1 `
  -Name dflippotower-agent-harness-review-1 `
  -TaskName AgentHarness-GitHubRunner-Review-1 `
  -Labels agent-harness-review
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token `
  -InstallDir D:\Agents\github-runner-review-2 `
  -Name dflippotower-agent-harness-review-2 `
  -TaskName AgentHarness-GitHubRunner-Review-2 `
  -Labels agent-harness-review
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token `
  -InstallDir D:\Agents\github-runner-review-3 `
  -Name dflippotower-agent-harness-review-3 `
  -TaskName AgentHarness-GitHubRunner-Review-3 `
  -Labels agent-harness-review
```

Each call consumes the token; request a new one for every member. The token is never saved by the installer.

To add a fourth member, repeat the same pattern with unused `-InstallDir` / `-Name` / `-TaskName` values and the same
label. To repair or remove a member, delete the runner in GitHub (**Settings > Actions > Runners**), uninstall the
matching scheduled task, and delete that member's install directory, then reinstall with the snippet above if you are
repairing it:

```powershell
Unregister-ScheduledTask -TaskName AgentHarness-GitHubRunner-Review-1 -Confirm:$false
Remove-Item -LiteralPath D:\Agents\github-runner-review-1 -Recurse -Force
```

Replace `-1` with the member you are removing. The same GitHub-delete + uninstall-task + delete-install-dir sequence
is the repair path used for the tower runner.

## Staging smoke slot

`.github/workflows/staging.yml` keeps one disposable Agent Harness Server slot on the same tower, so a candidate
branch or pull-request head can be opened in a browser before it is merged. It is deliberately not a second
production: no GPU, no provider spend, no household services, and no production data or credentials.

### Trust model

Trusted code only. Isolation exists to prevent **accidents** — the wrong checkout, the wrong port, killing the
production task, retagging production images — not to sandbox hostile code. Candidate Python is **not** treated as
hostile, which is the only reason staging may share the owner's Windows account and host with production.

| Allowed | Not allowed |
| --- | --- |
| Branches in `dflippojr/agent-harness` | Fork pull requests and other repositories |
| Pull requests whose **head repository is `dflippojr/agent-harness`** | Untrusted-PR or dedicated-VM isolation |

`scripts/resolve_staging_ref.py` enforces that on a GitHub-hosted runner in the `resolve` job, before the staging
runner checks out or runs anything: a fork head, a missing branch, a non-numeric `pr_number`, a ref that is not a
commit, or both inputs at once fails the run there. The control plane is always the workflow file on `main`; a
dispatch from any other ref is refused, and the candidate's own `deploy-ci.ps1` is never the deployer.

### Locations

Nothing below is derived by appending `-staging` at runtime. `ops/harness/staging-common.ps1` spells every staging
location out and `Assert-StagingTarget` refuses any path that is, or is inside, a production location.

| Role | Production (never a staging target) | Staging |
| --- | --- | --- |
| Checkout | `D:\Projects\agent-harness` | `D:\Projects\agent-harness-staging` |
| Data (SQLite, workspaces, transcripts, managed config) | `D:\Agents\harness` | `D:\Agents\harness-staging` |
| Config | repo `config/` + untracked local | candidate `config/` + `D:\Agents\harness-staging\harness.local.yaml` |
| Virtual environment | `.venv` / `.venv-path` | `D:\Agents\harness-staging\venv` |
| Logs | `D:\Agents\harness\logs` | `D:\Agents\harness-staging\logs` |
| Bind | `127.0.0.1:8100` | `127.0.0.1:8101` |
| Tailscale Serve | `:443` (`ops/tailscale/serve.ps1`) | `:8444` (`ops/tailscale/serve-staging.ps1`) |
| Scheduled task | `AgentHarness-Daemon` | `AgentHarness-Daemon-Staging` |
| GitHub environment | `tower-production` | `tower-staging` (no production secrets) |
| Runner | `agent-harness-tower-linux` | `agent-harness-staging` |

`tower-staging` holds no secrets at all today; the staging job needs only `contents: read`. Restrict the environment
to this repository's default branch so a candidate copy of the workflow cannot claim it.

The production stop matcher in `ops/harness/restart-daemon.ps1` and the staging one in
`ops/harness/restart-daemon-staging.ps1` exclude each other: a staging command line always names the
`harness-staging` data root and a production one never does. Staging stops its own scheduled task and its own port
only, never "any Python on 8100". Staging runs no `docker` command in v1, so `agent-harness-sandbox:py312` and
`agent-harness-cli:1` cannot move; if a local sandbox tag is ever needed it is `agent-harness-sandbox:staging`.

### Capabilities

`ops/harness/staging-profile.yaml` is copied over the candidate's `config/profile.yaml` on every deploy. The loader
applies `profile.yaml` after both `harness.yaml` and `harness.local.yaml`, so the candidate's own yaml cannot switch
a forced-off module back on. It selects the `full` profile with **every** module off — narrower than the `service`
profile, which would require an enabled hosted backend. Off: local model / llama-server / inference endpoint,
images and image edit, the GPU guard, hosted CLI backends, `homelab`, `remote_control`, Mac `runners`, `jobs`,
notifications / ntfy, `backup`, `web` search and fetch, memory-library writes, `search`, `skills`, and repository
cloning. On: the Server process, Stables (the release the candidate pins in `tools/stables/pin.json`, checked out
by the workflow and copied into the staging checkout as `stables/`), `/health`, the owner/admin API against staging data, the config
registry over the staging overlay, and creating or listing sessions, which fail closed with a "no backend" error.

After the slot reports healthy, the deployer reads `/health` and refuses to leave staging up if any module or hosted
backend is enabled. `/health` also carries `build.commit` (from `HARNESS_BUILD_COMMIT`, set by the staging
supervisor, and by the production supervisor from the 40-hex SHA in the `.venv-path` target; unset for a dev
checkout), which is how you confirm the resolved SHA is the one running.

### Dispatch

The supported Python floor is `requires-python = ">=3.12"` in `pyproject.toml`. Staging uses Python 3.12,
matching CI and production. During a deploy, after stopping the staging slot, `deploy-staging.ps1` calls
`staging-python.ps1` to rebuild a missing or incompatible venv with `uv venv --python 3.12 --seed --clear`.
The staging runner needs `uv` on PATH (it can download Python 3.12); alternatively, pass `-BootstrapPython`
with the full path to a Python 3.12 interpreter. Compatible venvs are preserved. Rebuild only through the
staging scripts; after merging a Python-version change, the owner or dispatcher must redeploy staging.

Manual `workflow_dispatch` only; there is no pull-request label auto-deploy. Set exactly one of `branch` or
`pr_number`, or dispatch `reset` on its own. The SHA is resolved at job start and printed: if the pull-request head
moves afterwards the run still deploys the SHA it resolved, and says so. There is one slot, so a successful dispatch
replaces whatever was running. Closing or merging the pull request does not stop or reset the slot.

```powershell
gh workflow run staging.yml -f pr_number=N
gh workflow run staging.yml -f branch=feat/84-chat-home
gh workflow run staging.yml -f reset=true
```

Production never waits on staging: `staging.yml` uses its own `agent-harness-staging` concurrency group, so
`deploy-tower` (`agent-harness-main-deployment`) is never queued behind it, and staging never runs on
`agent-harness-tower-linux` or `agent-harness-ci`. If host contention ever forces a choice, cancel the staging run;
production is never cancelled or delayed for staging. The staging job is capped at **30 minutes**, after which it
fails closed with production unchanged. A healthy staging daemon then stays up until it is replaced or reset.

### Authentication

`:8444` is Tailscale Serve, tailnet-only, never Funnel. `D:\Agents\harness-staging\harness.local.yaml` admits
exactly one caller: the machine owner's Tailscale login. Guests and household members are not admitted in v1. The
deployer creates that file from `ops/harness/staging-harness.local.yaml` when it is missing and then **fails closed**
until `allowed_logins` names the owner — an empty list would make every tailnet login an owner. It also refuses an
overlay that references a production path or port. Production's `harness.local.yaml`, secret files, and SQLite
database are never copied.

Each deploy and each reset mints a fresh staging-only owner token (`scripts/staging_owner_token.py`) and revokes the
previous one. The secret is never printed into the Actions log; read it on the tower with:

```powershell
Get-Content D:\Agents\harness-staging\owner-token.txt
```

Pair by opening `https://<tower>.<tailnet>.ts.net:8444/` in a browser that is already on the tailnet. Do not point
Agent Harness for Mac or the production PWA at staging; v1 is browser smoke against the staging origin.

### Reset

`gh workflow run staging.yml -f reset=true` (or `ops\harness\reset-staging.ps1` on the tower) stops
`AgentHarness-Daemon-Staging` and deletes the variable state the candidate created, leaving the slot stopped. A
deploy performs the same clean first, so every deploy starts from empty session and project state.

- **Deleted:** everything directly under `D:\Agents\harness-staging` except the entries below — SQLite and its WAL
  files, workspaces, transcripts, the managed-config overlay, artifacts, images, the recorded SHA, and the staging
  owner token. Deleting the database is what rotates staging tokens: previous staging cookies and tokens stop working.
- **Preserved:** `harness.local.yaml`, `logs`, and `venv`, plus the staging checkout. Reset is not uninstall, and no
  Docker image is removed.
- **Not seeded.** v1 copies no fixture from production.

### Tower setup

Once per tower, as the owner (not from an Actions job):

```powershell
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token -Labels agent-harness-staging `
  -InstallDir D:\Agents\github-runner-staging -Name dflippotower-agent-harness-staging `
  -TaskName AgentHarness-GitHubRunner-Staging
D:\Projects\agent-harness\ops\harness\install-task-staging.ps1   # registers AgentHarness-Daemon-Staging
D:\Projects\agent-harness\ops\tailscale\serve-staging.ps1        # publishes :8444 -> 8101, leaving :443 alone
```

Install the task from the **production** checkout, not the staging one: the scheduled task remembers the supervisor
path it was registered with, and that supervisor must stay trusted `main` code even though everything it starts and
writes is staging. Python must be on the runner account's `PATH`; the first deploy creates
`D:\Agents\harness-staging\venv` with it and later deploys reuse it.

Then dispatch once, edit `D:\Agents\harness-staging\harness.local.yaml` when the first run tells you to set
`allowed_logins` (and set `public_url` to `https://<tower>.<tailnet>.ts.net:8444` so the run summary links straight
to the slot), and dispatch again.

### Real-tower checklist

1. `gh workflow run staging.yml -f pr_number=N`, then read the run summary for the resolved SHA and the URL.
2. Open `https://<tower>.<tailnet>.ts.net:8444/` and confirm `(Invoke-RestMethod .../health).build.commit` is that SHA.
3. Confirm production is untouched: `https://<tower>.<tailnet>.ts.net/health` still answers, `Get-ScheduledTask
   AgentHarness-Daemon` is still running, `git -C D:\Projects\agent-harness rev-parse HEAD` is still the deployed
   `main` commit, and `docker image inspect agent-harness-sandbox:py312` has the same image id as before.
4. Replace the slot with another ref and confirm the SHA in `/health` changes while production's does not.
5. `gh workflow run staging.yml -f reset=true`, then confirm `:8101` is down, `D:\Agents\harness-staging` holds only
   `harness.local.yaml`, `logs`, and `venv`, and the previous staging token no longer authenticates.
6. Deploy again and confirm `Get-Content D:\Agents\harness-staging\owner-token.txt` is a different token.

### Staging failure behavior

- Rejected ref (fork head, missing branch, both inputs, dispatch off `main`): the run fails in `resolve` and the
  tower does nothing.
- Clone, fetch, dependency, overlay, health, or capability-check failure: the staging daemon is stopped and the run
  fails. Production's checkout, SHA, process, `/health` on 8100, data, credentials, stable Docker tags, GPU, and
  `:443` route are unchanged either way — no staging step writes to any of them.
- Missing `AgentHarness-Daemon-Staging` task: the deploy fails with the `install-task-staging.ps1` instruction rather
  than starting anything by hand.
- To recover the slot from any state, dispatch `reset=true` and then deploy again.

## Failure behavior

- CI failure, cancellation, pull-request run, or non-`main` run: no images and no deployment.
- A docs-only push (`test` skipped) or a commit that is no longer `main`'s tip: `guard` stops it, so no images and
  no deployment.
- Image build/push failure: no deployment. Actions cache export failure is ignored because the cache is optional.
- Docker unavailable, live checkout dirty, wrong branch, non-fast-forward history, or dependency resolution failure:
  deployment fails visibly before the daemon is stopped or the live checkout and virtual environment are changed.
- After the daemon is stopped, a swap, fast-forward, image retag, restart, or health-check failure triggers rollback:
  the partially deployed daemon is stopped, the previous checkout and untouched virtual environment are restored, and
  the previous daemon is started and health-checked. The Actions job still exits non-zero and reports whether rollback
  itself had any errors.
- On Linux the rollback confirms with `systemctl --user is-active` that the unit stopped, retrying the stop up to three
  times, before it restores anything: the unit's supervisor reads `.venv-path` only when it starts, so one that never
  stopped would keep the new environment, and a `start` on its still-active unit would be a no-op. If the unit will
  not stop, the rollback still restores the files and then restarts the unit, so a fresh supervisor reads the restored
  pointers. If that restart fails too, the new environment and its Stables copy are kept for the supervisor still
  running them (it read both pointers when it started), and the job fails naming the unit's state, the
  `systemctl --user restart` that finishes the rollback, and the two directories to remove after it.
- Rerunning a commit that is already live (a re-run of its workflow): if the checkout, `.venv-path`, `.stables-path`,
  the stable image tags and, on Linux, the supervisor and an active unit all match it, the interpreter and the Stables
  `index.html` those pointers name both exist, and `/health` answers, the deployment reports that nothing needs doing
  and succeeds without stopping anything. Otherwise it fails, naming what differs or is missing, without stopping the
  unit, and never removes or restages the active environment: that commit's environment and Stables copy are the ones
  the running daemon uses, so a redeploy over them is never safe. Recovery is by hand, and a rerun repeats the same
  check. A side-by-side environment left by an interrupted run of a commit that is not live still stops a deployment
  of that commit; remove it by hand.
- A newer `main` commit superseding an older queued deployment makes the older deployment exit without changing the
  tower; the serialized newer workflow run handles it.
