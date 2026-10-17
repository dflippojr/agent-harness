# Stables UI tests

Run from the repository root with Python dependencies installed and a checkout of the pinned Stables release
(`tools/stables/pin.json`): set `HARNESS_STABLES_DIR` to its `public/` directory, or clone it into this checkout as
`stables/`. CI checks the tag out into `stables-release/`.

```
npm --prefix tools/ui-tests ci
npm --prefix tools/ui-tests exec -- playwright install chromium webkit
npm --prefix tools/ui-tests test -- --workers=1
```

The suite starts its own loopback fake-model instance on 18800/18801, refuses existing servers, and removes its synthetic data on completion. Set PYTHON for another interpreter and HARNESS_UI_INSTANCE_DIR for another disposable directory. Tests create unique entities and use one worker; each page gets fresh browser storage and signs in through this instance's /connect.

See [the testing contract](../../docs/ui-testing.md) for fixtures, journeys, isolation and manual teardown. Reports and traces stay in ignored playwright-report/ and test-results/. Trace ZIP resources and authorization values are redacted before CI upload. Start stdout is suppressed because it contains disposable tokens. node --test tooling.test.mjs verifies redaction with synthetic tokens.

The first pass's defects (#591–#595) are fixed, and `defects.spec.ts` holds their regression tests. The second pass (#625) added `redesign.spec.ts` for the flows it found passing. Its defects, #626–#634, were one class, and `layout-invariants.spec.ts` judges every phone surface listed in `tests/inventory.ts` for it, in portrait and in the two landscape projects (#734). The suite has no retries. The `signedIn` fixture in `tests/fixtures.ts` skips the projects a test does not need through `onlyProjects`, before it opens `/connect`: behaviour journeys and defect regressions run in the light projects only, the self-sizing reflow tests run in `desktop-light` and `iphone-se-light`, and the layout invariants run on the phones in portrait and landscape (the contract's "Which journeys run where" lists every case). Physical notch/keyboard/install behavior, household/guest roles, and HTTPS service-worker behavior require the separate real-device environment documented in the contract.
