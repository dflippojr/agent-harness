import { test, expect, openSession, expectDone, expectReflow, reflowTest } from './fixtures';

const surfaces = [
  { name: 'Chat landing', route: '/#/chat', ready: '.chat-welcome' },
  { name: 'Agents', route: '/#/agents', ready: '.agent-row' },
  { name: 'New task', route: '/#/new', ready: '.new-task-page' },
  { name: 'Jobs', route: '/#/jobs', ready: '.job-row' },
  { name: 'Job form', route: '/#/jobs/new', ready: '#job-name' },
  { name: 'Profile', route: '/#/profile', ready: 'a' },
  { name: 'Settings', route: '/#/settings', ready: '.version-row' },
  { name: 'Appearance', route: '/#/profile/appearance', ready: '.theme-choice' },
  { name: 'Connection', route: '/#/profile/connection', ready: 'input' },
];

reflowTest('primary pages reflow without overflow at 320/375/768/1440 CSS pixels', async ({ signedIn: page }, info) => {
  const scheme = info.project.use.colorScheme;
  await expect.poll(() => page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim())).toBe(scheme === 'dark' ? '#000000' : '#f3f4f6');
  for (const surface of surfaces) {
    await test.step(surface.name, async () => {
      await page.goto(surface.route);
      await expect(page.locator(surface.ready).first()).toBeVisible();
      await expectReflow(page);
    });
  }
  await openSession(page, 'Protected settings');
  await expect(page.getByRole('region', { name: 'Approval needed', exact: true })).toBeVisible();
  await expectReflow(page);
  await openSession(page, 'Tool rows');
  await expectDone(page);
  const sessionUrl = page.url();
  for (const tab of ['', '/changes', '/info']) {
    await page.goto(sessionUrl + tab);
    await expect(page.locator('.session-tabs')).toBeVisible();
    if (!tab) await expectDone(page);
    else if (tab === '/changes') await expect(page.getByText('No git repositories in this workspace yet.', { exact: true })).toBeVisible();
    else await expect(page.locator('main')).toContainText('Tool rows');
    await expectReflow(page);
  }
});

test('live resize keeps partially filled composer, selected tab, menu and focus', async ({ signedIn: page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    // Optional disabled-module probes yield expected browser 404 console diagnostics.
    if (message.type() === 'error' && !message.text().includes('Failed to load resource: the server responded with a status of 404')) errors.push(message.text());
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await openSession(page, 'Tab bar tour');
  await expectDone(page);
  const url = page.url();
  const composer = page.getByPlaceholder('Continue this session…');
  await composer.fill('Partly typed follow-up');
  const menuButton = page.getByRole('button', { name: 'Session menu', exact: true });
  await menuButton.click();
  const menu = page.getByRole('menu', { name: 'Session', exact: true });
  for (const width of [1024, 768, 600, 375, 768, 1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(composer).toHaveValue('Partly typed follow-up');
    await expect(page.locator('.session-tabs button.on')).toHaveText('Transcript');
    await expect(menu).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Rename', exact: true })).toBeFocused();
    await expect(page).toHaveURL(url);
  }
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('menuitem', { name: 'Run again as new session' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(menu).toBeHidden();
  await expect(menuButton).toBeFocused();
  await composer.click();
  await expect(composer).toBeFocused();
  expect(errors).toEqual([]);
});
