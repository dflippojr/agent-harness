# Agent Harness Web UI tests

Run from the repository root with Python dependencies installed:

```
npm --prefix tools/ui-tests ci
npm --prefix tools/ui-tests exec -- playwright install chromium webkit
npm --prefix tools/ui-tests test -- --workers=1
```

The suite starts its own loopback fake-model instance on 18800/18801, refuses existing servers, and removes its synthetic data on completion. Set PYTHON for another interpreter and HARNESS_UI_INSTANCE_DIR for another disposable directory. Tests create unique entities and use one worker; each page gets fresh browser storage and signs in through this instance's /connect.

See [the testing contract](../../docs/ui-testing.md) for fixtures, journeys, isolation and manual teardown. Reports and traces stay in ignored playwright-report/ and test-results/. Trace ZIP resources and authorization values are redacted before CI upload. Start stdout is suppressed because it contains disposable tokens. node --test tooling.test.mjs verifies redaction with synthetic tokens.

Known failed exploratory cases are #591 (Chat landing overflow at 320px), #592 (New task label associations), #593 (tool actions covered in short phone landscape), #594 (job Enabled target height), and #595 (phone session-tab spacing). This PR locks independently passing flows; those defects receive regression tests with their separate fixes. It contains no skipped cases or retries. Physical notch/keyboard/install behavior, household/guest roles, and HTTPS service-worker behavior require the separate real-device environment documented in the contract.
