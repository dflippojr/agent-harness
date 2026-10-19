// The verify skill's drive step (.claude/skills/verify): the smoke journey and the requested features against the
// instance the skill already started, so there is no webServer here. VERIFY_WEB_URL and VERIFY_EVIDENCE_DIR come from
// verify.py. The main suite's config (playwright.config.ts) does not pick these tests up.
import path from 'node:path';
import { defineConfig, devices } from '@playwright/test';

const web = process.env.VERIFY_WEB_URL ?? '';
// docs/ui-testing.md, "Scope and isolation": a disposable loopback instance only, never the live harness.
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(web) || web.endsWith(':8100')) {
  throw new Error('VERIFY_WEB_URL must be the http://127.0.0.1:<port> Web URL of a fake-model instance; run verify.py');
}
const evidence = process.env.VERIFY_EVIDENCE_DIR ?? 'test-results/verify';

export default defineConfig({
  testDir: './verify', fullyParallel: false, workers: 1, retries: 0,
  timeout: 90_000, expect: { timeout: 15_000 },
  reporter: [['list']],
  outputDir: path.join(evidence, 'playwright'),
  use: { baseURL: web, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  // One desktop and one phone, both Chromium, so the skill needs only one browser.
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'pixel7', use: { ...devices['Pixel 7'] } },
  ],
});
