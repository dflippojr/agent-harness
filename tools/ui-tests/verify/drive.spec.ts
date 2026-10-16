// The verify skill's journeys (.claude/skills/verify/SKILL.md, "Feature map"). Each test is tagged with its feature,
// so `--grep @smoke|@task` drives just those. Screenshots go to VERIFY_EVIDENCE_DIR/screenshots.
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { test as base, expect, type Page, type TestInfo } from '@playwright/test';
import { createTask, expectDone } from '../tests/fixtures';

const shots = path.join(process.env.VERIFY_EVIDENCE_DIR ?? 'test-results/verify', 'screenshots');

// Page errors and console errors fail a journey. Fake mode drops optional modules (images), whose probes log 404s.
const test = base.extend<{ errors: string[] }>({
  errors: async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => {
      if (message.type() === 'error' && !message.text().includes('Failed to load resource: the server responded with a status of 404')) errors.push(message.text());
    });
    await use(errors);
  },
});

async function capture(page: Page, info: TestInfo, name: string) {
  await page.screenshot({ path: path.join(shots, `${info.project.name}-${name}.png`) });
}

// docs/ui-testing.md, "Seed and sign-in": /connect signs this copy in as the owner.
async function signIn(page: Page) {
  await page.goto('/connect');
  await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
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
