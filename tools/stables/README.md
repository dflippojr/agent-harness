# The pinned Stables release

Stables, the browser client, is the owner's private add-on and lives in its own private repository (#334). This
directory pins the release the Server uses:

- `pin.json`: the repository, tag and commit.
  - The tower deploy checks that tag out (`.github/workflows/ci-cd.yml`), and `ops/harness/deploy-ci.ps1` serves it
    through `HARNESS_STABLES_DIR`.
  - CI's `ui-tests` job runs the Playwright journeys in `tools/ui-tests` against it.
- `api-calls.json`: what the Server's tests need from that release, generated from it. It holds every `api(...)`
  call and path literal in `public/`, the browser storage keys, the build id and the snippet languages. The
  management-parity tests (`tests/test_management_parity.py`, `tests/test_web_no_daemon_admin.py`), the snippet
  languages check and the build-id check read it through `tests/stables_pin.py`.
- `harness/compat.py`'s `WEB_BUILD_ID`: the build id the Server advertises to browsers. Pinning writes it from the
  release's `public/client.mjs`, the same value `api-calls.json` records, so a re-pin can't leave it stale (#996).
  Don't edit it by hand: `tests/test_version_compat.py` fails when the two differ.

Both CI and the deploy job read the private repository with `STABLES_DEPLOY_KEY`, a read-only deploy key on
dflippojr/stables. It is stored in this repository's Actions and Dependabot secrets.

## Moving to another release

1. Tag the release in dflippojr/stables (`vX.Y.Z`) and push the tag.
2. Check that tag out, then pin it from this repository's root:

   ```sh
   python tools/stables/pin.py <path to the Stables checkout>
   ```

3. Commit `pin.json`, `api-calls.json` and `harness/compat.py`. When the client calls a new owner endpoint, the
   parity tests name it, and `docs/management-parity.md` needs its row.

CI runs `python tools/stables/pin.py --check stables-release` against its checkout of the tag. It fails when the
tag no longer points at the pinned commit, or when `api-calls.json` or compat.py's build id does not match the
release.
