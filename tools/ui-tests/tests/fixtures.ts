import { test as base, expect, type Page } from '@playwright/test';
export const test = base.extend<{ signedIn: Page }>({
  signedIn: async ({ page }, use) => {
    await page.goto('/connect');
    await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
    await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
    try { await use(page); }
    finally { await page.context().setOffline(false); }
  },
});
export { expect };
export async function openSession(page: Page, title: string) {
  await page.goto('/#/agents');
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill(title);
  await page.locator('a.card[data-split-key]').filter({ has: page.getByRole('heading', { name: title, exact: true }) }).first().click();
  await expect(page.locator('.session-tabs')).toBeVisible();
}
export async function expectDone(page: Page) {
  await expect(page.locator('.session-strip .badge').first()).toHaveText('done');
  await expect(page.locator('main .final').last()).toBeVisible();
}
export async function createTask(page: Page, title: string, prompt: string) {
  await page.goto('/#/new');
  await expect(page.getByRole('region', { name: 'New task', exact: true })).toBeVisible();
  await page.getByLabel('Prompt', { exact: true }).fill(prompt);
  // Project and Title lack associated labels in the existing UI (tracked by the UI pass).
  await page.locator('#new-task-form select').first().selectOption('ui');
  await page.getByPlaceholder('Optional; defaults to the first line').fill(title);
  await page.getByRole('button', { name: 'Start', exact: true }).click();
  await expect(page).toHaveURL(/#\/s\/[^/]+$/);
  await expect(page.locator('.session-tabs')).toBeVisible();
  return page.url();
}
export async function audit(page: Page, query: Record<string, string>) {
  return page.evaluate(async query => {
    const url = new URL('/api/admin/v1/audit', localStorage.getItem('harness.daemonUrl')!);
    for (const [key, value] of Object.entries(query)) url.searchParams.set(key, value);
    const response = await fetch(url, { headers: {
      Authorization: 'Bearer ' + localStorage.getItem('harness.ownerToken'),
      'X-Agent-Harness-Client': 'cli/2',
    } });
    if (!response.ok) throw new Error('Audit read failed: ' + response.status);
    return (await response.json()).items;
  }, query);
}
