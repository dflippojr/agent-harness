// The verify skill's journeys (.claude/skills/verify/SKILL.md, "Feature map"). Each test is tagged with its feature,
// so `--grep @smoke|@task` drives just those. Screenshots go to VERIFY_EVIDENCE_DIR/screenshots.
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test as base, expect, type Page, type TestInfo } from '@playwright/test';
import { createTask, expectDone, signIn } from '../tests/fixtures';

const shots = path.join(process.env.VERIFY_EVIDENCE_DIR ?? 'test-results/verify', 'screenshots');

// Fake mode drops the local-model modules (docs/ui-testing.md, "How isolation is enforced"); Stables probes these and
// gets 404. Every other failed response fails the journey.
const EXPECTED_404 = ['/api/admin/v1/gpu', '/api/admin/v1/models/status'];
const LOAD_FAILED = 'Failed to load resource: the server responded with a status of ';

// Page errors, console errors and failed responses fail a journey. The browser logs each failed response as a console
// error too; those are judged by the response, so only the expected 404 probes pass.
const test = base.extend<{ errors: string[] }>({
  errors: async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      const expected = response.status() === 404 && EXPECTED_404.includes(new URL(response.url()).pathname);
      if (response.status() >= 400 && !expected) errors.push(`HTTP ${response.status()} ${response.request().method()} ${response.url()}`);
    });
    page.on('console', message => {
      if (message.type() === 'error' && !message.text().startsWith(LOAD_FAILED)) errors.push(message.text());
    });
    await use(errors);
  },
});

async function capture(page: Page, info: TestInfo, name: string) {
  await page.screenshot({ path: path.join(shots, `${info.project.name}-${name}.png`) });
}

test('smoke: sign in, tab bar, Agents shows the seed @smoke', async ({ page, errors }, info) => {
  await signIn(page);
  await expect(page.locator('#boot-splash')).toHaveCount(0);
  for (const name of ['Chat', 'Agents', 'Jobs', 'Images', 'Profile']) {
    await expect(page.locator('#tab-bar').getByRole('link', { name: new RegExp(name) })).toBeAttached();
  }
  await capture(page, info, 'chat');
  await page.goto('/#/agents');
  await expect(page.getByRole('region', { name: 'Needs you', exact: true })).toBeVisible();
  const ask = page.locator('a.agent-row', { hasText: 'Protected settings' }).first();
  await expect(ask).toContainText('write_file protected/settings.json');
  await expect(page.getByRole('region', { name: 'Recent', exact: true })).toBeVisible();
  await capture(page, info, 'agents');
  expect(errors).toEqual([]);
});

test('task: a new task streams its answer and persists @task', async ({ page, errors }, info) => {
  await signIn(page);
  const title = `Verify ${info.project.name} ${randomUUID().slice(0, 8)}`;
  await createTask(page, title, 'Show me some markdown.');
  await expectDone(page);
  await expect(page.getByRole('heading', { name: 'Summary', exact: true })).toBeVisible();
  await page.reload();
  await expectDone(page);
  await expect(page.locator('#title')).toHaveText(title);
  await capture(page, info, 'task');
  expect(errors).toEqual([]);
});

// The filter itself (#744 review): a missing stylesheet fails a journey even though the page still renders; the
// expected module probes do not.
test('a failed response other than the expected probes is an error @smoke', async ({ page, errors }, info) => {
  base.skip(info.project.name !== 'desktop', 'one run is enough');
  await page.goto('/connect');
  await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
  await page.evaluate(async probes => { for (const probe of probes) await fetch(probe).catch(() => {}); }, EXPECTED_404);
  expect(errors).toEqual([]);
  await page.evaluate(() => new Promise(resolve => {
    const link = Object.assign(document.createElement('link'), { rel: 'stylesheet', href: '/verify-missing.css' });
    link.onerror = link.onload = resolve;
    document.head.append(link);
  }));
  await expect.poll(() => errors.some(error => error.startsWith('HTTP 404 GET') && error.endsWith('/verify-missing.css'))).toBe(true);
  expect(errors.filter(error => !error.endsWith('/verify-missing.css'))).toEqual([]);
});
