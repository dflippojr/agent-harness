import { test as base, expect, type Page, type TestInfo } from '@playwright/test';
// Behaviour journeys and defect regressions assert nothing that depends on the theme, so they run in the light projects.
const LIGHT = ['desktop-light', 'iphone-se-light', 'pixel7-light'];
// Tests that set their own viewport sizes need one mouse and one touch (WebKit) run; pixel7 adds nothing for them.
const SELF_SIZING = ['desktop-light', 'iphone-se-light'];
export function onlyProjects(info: TestInfo, names: readonly string[]) {
  base.skip(!names.includes(info.project.name), 'theme-independent');
}
// The skip runs before the sign-in navigation, so a project a test does not need costs a page and no server requests.
const signedInOn = (names: readonly string[]) => base.extend<{ signedIn: Page }>({
  signedIn: async ({ page }, use, info) => {
    onlyProjects(info, names);
    await page.goto('/connect');
    await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
    await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
    try { await use(page); }
    finally { await page.context().setOffline(false); }
  },
});
export const test = signedInOn(LIGHT);
export const selfSizingTest = signedInOn(SELF_SIZING);
// The one dark run: the primary-page sweep also asserts each scheme's --bg.
export const reflowTest = signedInOn([...SELF_SIZING, 'desktop-dark', 'iphone-se-dark']);
export { expect };
export async function openSession(page: Page, title: string) {
  await page.goto('/#/agents');
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.fill(title);
  await page.locator('a.card[data-split-key]').filter({ has: page.getByRole('heading', { name: title, exact: true }) }).first().click();
  await expect(page.locator('.session-tabs')).toBeVisible();
}
// No horizontal overflow at the four reflow widths, each measured after two animation frames.
export async function expectReflow(page: Page) {
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    await expect.poll(() => page.evaluate(() => innerWidth)).toBe(width);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
}
export async function expectDone(page: Page) {
  await expect(page.locator('.session-strip .badge').first()).toHaveText('done');
  await expect(page.locator('main .final').last()).toBeVisible();
}
export async function createTask(page: Page, title: string, prompt: string) {
  await page.goto('/#/new');
  await expect(page.getByRole('region', { name: 'New task', exact: true })).toBeVisible();
  await page.getByLabel('Prompt', { exact: true }).fill(prompt);
  await page.getByLabel('Project', { exact: true }).selectOption('ui');
  await page.getByLabel('Title', { exact: true }).fill(title);
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
