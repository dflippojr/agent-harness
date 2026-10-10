# GitHub CI and container images

`.github/workflows/ci.yml` is the single test workflow, and `.github/workflows/ci-cd.yml` publishes and deploys only
after that workflow succeeds for the exact commit pushed to `main`:

1. **CI / test** runs the complete test suite once, with coverage, on GitHub-hosted `windows-latest` for pull
   requests and pushes to `main`, and uploads `coverage.xml` as an artifact. **CI / sonar** (same workflow,
   `needs: test`) feeds that coverage to SonarCloud. On a push to `main`, `sonar` is `continue-on-error`, so a failing
   quality gate shows on the job but does not block deployment; only failing tests do. On pull requests the quality
   gate still fails the check. Tests used to run a second time on a self-hosted `agent-harness-ci` pool on the
   tower; that pool is retired so CI no longer competes with llama-server for the tower's RAM.
2. **publish-images** is triggered by `workflow_run` only after `CI` completes successfully for a push to `main`.
   It checks out `github.event.workflow_run.head_sha`, never a branch name, and GitHub-hosted Linux builders publish
   both runtime images to GHCR:
   - `ghcr.io/dflippojr/agent-harness-sandbox:sha-<commit>` and `:py312`
   - `ghcr.io/dflippojr/agent-harness-cli:sha-<commit>` and `:1`
3. **deploy-tower** runs only after image publication for that same tested SHA. The repository-scoped runner pulls the
   immutable images, verifies that the live checkout is clean `main` and can fast-forward, builds a fresh side-by-side
   virtual environment, stops the daemon, swaps environments, fast-forwards, retags the images, starts the daemon, and
   waits for `/health`.

The SHA tags are immutable deployment inputs; the stable tags match the local names already expected by the daemon.
The CLI Dockerfile accepts `SANDBOX_IMAGE` so its hosted build is based on the exact sandbox image from the same commit.
The supervisor reads the ignored `.venv-path` file when present. Deployment switches that pointer rather than moving
the newly built environment, so Windows entry points keep their original absolute paths; the original `.venv` remains
untouched for first-deployment rollback. Later successful deployments remove the superseded managed environment.
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

`.github/workflows/docs-regen.yml` runs on a push to `main` that touches `docs/fragments/**`, `scripts/docs/**`, `harness/**` or `harness_modules/**` (the code-derived tables read routes and settings from there)
(and on `workflow_dispatch`). It runs `python scripts/docs/build.py` and, only if a generated region changed, commits
`Docs: regenerate fragment regions [skip ci]` to `main` as `github-actions[bot]`. It is the single writer of generated
regions; PRs add fragments only. It uses the default `GITHUB_TOKEN` with `contents: write` on that one job.

- **No deploy, no loop.** A push made with `GITHUB_TOKEN` starts no workflow runs, so the bot commit starts neither
  `docs-regen`, `CI`, nor (through `CI`) `deploy-tower`. The `[skip ci]` marker repeats that explicitly, and the job
  skips any head commit carrying it. `tests/test_ci_cd_workflow.py` pins both.
- **Bursts.** The `docs-regen` concurrency group cancels an older run, so close-together merges produce one commit.
  If `main` moves during a run, the push is retried from the new tip, up to three times.
- **Main CI.** After a fragment PR merges, the region lags until the bot commit lands, so the `Check docs fragments`
  step on main runs `--check --fragments-only` (schema only). PRs still compare regions against the base.
- **Nothing changed** means no commit (`tests/test_docs_build.py::test_build_twice_changes_nothing_the_second_time`).
- `main` has no branch protection today. If a ruleset ever blocks the bot push, the owner must allow
  `github-actions[bot]` to bypass it for this workflow.

## Deployment boundary

The daemon itself is deliberately not containerized. It is a host Python process because it coordinates Windows
scheduled tasks, local repository paths, Docker sandbox creation, GPU/model controls, and native provider logins.

The owner explicitly approved the repository-scoped self-hosted runners. The `agent-harness-tower` runner gives
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
and do not contribute coverage. Non-Python trees (`harness/web`, `ops`, scripts that are not `.py`) are excluded
from the coverage metric. Confirm the SonarCloud project still uses a gate that includes coverage on new code;
the workflow cannot set that condition itself. PR analysis stays on GitHub-hosted Windows; it must not move to
the tower.

Serial `sonar` job baseline from nine successful hosted runs on 2026-09-23 (createdAt 02:47–19:42 UTC): job
wall-clock median 818 s, worst 1026 s; `Run tests with coverage` median 684 s, worst 843 s (the 828 s figure in
issue #191 is run 35911097047). Pushing a branch without a pull request does not trigger `ci.yml`, and the
`paths-ignore` allow-list that skips `test` skips `sonar` with it (none of those files are Sonar sources).

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

The review has two parts under fixed headings. `## Findings` lists correctness bugs only; it alone sets the verdict, the
check conclusion and the inline annotations. `## Style and structure (advisory)` lists at most five suggestions on naming,
responsibilities, duplication, consistency with the neighbouring code and simplifications, judged against the repository's
own lint and format configuration. It never changes the verdict or fails the check. Its items are written without the
`path:line` form, and the annotation scanner also stops reading at this heading, so they cannot become annotations. A clean review can therefore still carry advisory suggestions.

The check summary carries the posted comment, so findings are readable from the Checks tab. The workflow run itself
stays green when the review merely has findings: a red run means the review machinery broke, a red check means the
code has findings. The prompt requires the reviewer to end with exactly two lines, `REVIEW_VERDICT: CLEAN` or
`REVIEW_VERDICT: FINDINGS <n>`, then `REVIEW_STATUS: COMPLETE`. The verdict only counts as the line directly above the
status marker, so verdict-like text quoted from the diff cannot spoof it; a missing or malformed verdict is treated like
a missing marker. Both lines are removed before posting. The check is created on the head commit that the job checked
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

All three CLIs run under the review runner service user and must be logged in for that same user. Cursor uses ask mode
with its sandbox enabled on non-Windows hosts; the flag is skipped on Windows because that CLI does not support it.
Windows ask mode is not an OS sandbox; keep Cursor opt-in rather than adding it to the default Windows pool. Codex ignores the service user's configuration, restores only the required unelevated Windows
sandbox setting, disables apps and plugins, and supplies an empty MCP server table before entering its read-only sandbox.
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
  prefix (`\\server\`, `\\?\`, `\\.\`), or a `file:` URL.
- Any separator followed by a profile directory name (`Users`, `home`, `root` or `Documents and Settings`, including
  Windows trailing-period, trailing-space and short-name spellings) is rejected unless it is part of a known
  repository path. `docs/users.md` and `root-ca.md` are not profile directories; an unknown `examples/home/x.py` is
  rejected even though it is relative.

The rules run on the raw body and on what a reader would see: percent escapes, HTML entities, invisible format
characters, Unicode compatibility forms and Markdown backslash escapes are decoded first. This is intentionally
conservative: benign prose and examples matching a credential or path rule are also rejected, so the review prompt
tells the backend to cite only repository-relative paths and describe credentials in words. Quote authentication
scheme names in backticks (for example, `Bearer`) rather than printing a value-like sequence.

Known repository paths come from the Git index and every diff file, including deletions and both rename/copy sides.
Git-quoted paths are decoded as UTF-8 bytes, and patch and rename/copy metadata disambiguate filenames containing the
diff header's ` b/` separator. Exact citations of known paths (including `./` prefixes and Windows separators) are
masked for the profile-directory and long-token rules only; a known path never exempts an absolute root, a profile
prefix in front of it, or a credential pattern. Web links are split at `/` for the long-token rule, so a long
documentation URL is not one token. Validated Git metadata in the coverage marker is added only after the scan.
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

jobs:
  review:
    uses: dflippojr/agent-harness/.github/workflows/review.yml@review-v1
    with:
      pr_number: ${{ inputs.pr_number }}
      backend: ${{ inputs.backend }}
      mode: ${{ inputs.mode }}
      runs_on: '["self-hosted","Windows","X64","financial-planner-review"]'
      tooling_ref: review-v1
    secrets: inherit
```

Do not add a workflow-level `concurrency` group to the caller; the shared job already serializes per repository and
PR. Optional inputs: `max_diff_bytes` (otherwise the consumer's `REVIEW_MAX_DIFF_BYTES` variable, then `204800`) and
`tooling_ref` (use the trusted default branch or a maintainer-pinned tag, never a PR ref). The `REVIEW_*` repository variables are read from the consumer
repository.

**Runner label.** Register a self-hosted Windows runner for the consumer repo with
`ops/github/install-runner.ps1 -Labels <project>-review` under a service user whose Codex, Claude, and Cursor CLIs are
logged in, and pass that label in `runs_on`.

**Runner access (owner step).** Create a self-hosted runner group in GitHub and restrict it to the specific trusted
review/deploy workflows that need those runners, with workflow refs on the default branch or trusted tags. Labels
select runners but do not restrict who may schedule them. Keep fork-PR approval required for all outside contributors.
This GitHub administration step cannot be enforced by this repository change; the owner must configure it.

**Permissions.** The caller's `permissions` block must grant `contents: read`, `pull-requests: write` (PR comment), and
`checks: write` (the `Automated Code Review` check). A called workflow can only narrow these.

**Actions allowlist.** A consumer whose Actions policy allows only selected actions (for example GitHub-owned plus
`SonarSource/sonarqube-scan-action@*`) must also allow `dflippojr/agent-harness/.github/workflows/review.yml@*`
(Settings -> Actions -> General -> "Allow or block specified actions and reusable workflows").

**Version pin.** The `@review-v1` reference selects the workflow definition. The script follows trusted `main` by
default; pinning only the workflow does not stage script changes. For staged rollout, set `tooling_ref: review-v1`
as in the caller above so both workflow and script follow the same maintainer-controlled tag. After a change has
been verified here, the owner moves that tag deliberately:

```powershell
git tag -f review-v1 <verified commit on main>
git push -f origin refs/tags/review-v1
```

A breaking change to the caller contract (renamed or new required inputs) gets a new tag such as `review-v2`.

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

The pinned official runner is installed in `D:\Agents\github-runner`, with job work under its `_work` directory.
It is registered only to `dflippojr/agent-harness`, carries the custom
`agent-harness-tower` label, and starts at user logon through the hidden `AgentHarness-GitHubRunner` scheduled task.
Diagnostics live in `D:\Agents\github-runner\_diag`.

To repair or reinstall it, remove the runner in GitHub and its local install directory, obtain a fresh short-lived
registration token, then run:

```powershell
$gh = 'C:\Program Files\GitHub CLI\gh.exe'
$token = & $gh api -X POST repos/dflippojr/agent-harness/actions/runners/registration-token --jq .token
.\ops\github\install-runner.ps1 -Token $token
```

The token is never saved by the installer. GitHub stores the runner's own credential files in its install directory.

The live checkout must itself be on `main`; deployment intentionally fails closed on another branch or detached HEAD.
After first confirming that the checkout has no work that must be preserved, put it on `main` and fast-forward it:

```powershell
Set-Location D:\Projects\agent-harness
git status --short
git switch main
git fetch origin
git merge --ff-only origin/main
```

Do this as an explicit maintenance action, not from the Actions runner. Never discard local changes to make a deploy
pass.

## Review runner pool

Automated review (`.github/workflows/review.yml`) uses three repository-scoped self-hosted runners that share the
`agent-harness-review` label. Each member takes one job, so reviews of different PRs can run in parallel. They are
named, installed, and started as:

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
| Runner | `agent-harness-tower` | `agent-harness-staging` |

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
cloning. On: the Server process, Agent Harness Web, `/health`, the owner/admin API against staging data, the config
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
`agent-harness-tower` or `agent-harness-ci`. If host contention ever forces a choice, cancel the staging run;
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
- Image build/push failure: no deployment. Actions cache export failure is ignored because the cache is optional.
- Docker unavailable, live checkout dirty, wrong branch, non-fast-forward history, or dependency resolution failure:
  deployment fails visibly before the daemon is stopped or the live checkout and virtual environment are changed.
- After the daemon is stopped, a swap, fast-forward, image retag, restart, or health-check failure triggers rollback:
  the partially deployed daemon is stopped, the previous checkout and untouched virtual environment are restored, and
  the previous daemon is started and health-checked. The Actions job still exits non-zero and reports whether rollback
  itself had any errors.
- A newer `main` commit superseding an older queued deployment makes the older deployment exit without changing the
  tower; the serialized newer workflow run handles it.
