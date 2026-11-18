// Second pass (#625) over the mobile redesign (#506-#515): flows the #589 suite did not cover.
import { test, expect, openSession, expectDone, expectReflow, selfSizingTest } from './fixtures';
import type { Page } from '@playwright/test';

const settle = (page: Page) => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

// The contract's 60% height stands in for an on-screen keyboard; landscape swaps the project's portrait viewport.
function rotations(page: Page) {
  const { width, height } = page.viewportSize()!;
  return { portrait: { width, height }, landscape: { width: height, height: width } };
}

async function openSeededChat(page: Page) {
  await page.goto('/#/chat');
  await page.getByRole('region', { name: 'Recent chats', exact: true })
    .getByRole('link', { name: 'Hello from Chat. What can you do?' }).first().click();
  await expect(page).toHaveURL(/#\/chat\/[A-Za-z0-9_-]+$/);
  await expect(page.locator('.chat-feed')).toContainText('Hi! This is the fake model.');
}

selfSizingTest('Settings subpages and Actions tabs reflow at four widths and Back returns to Settings', async ({ signedIn: page }) => {
  await page.goto('/#/settings');
  await expect(page.locator('.version-row')).toBeVisible();
  const rows = await page.locator('a.set-row[data-setting]').evaluateAll(els => els.map(a => a.getAttribute('href')!));
  expect(rows).toEqual(expect.arrayContaining(['#/profile/notifications', '#/profile/backends', '#/actions/resources', '#/actions/disk']));
  for (const href of rows) {
    await test.step(href, async () => {
      await page.setViewportSize({ width: 375, height: 667 });
      await page.goto('/#/settings');
      await page.locator(`a.set-row[href="${href}"]`).click();
      await expect(page).toHaveURL(new RegExp(href.replace(/[/#]/g, '\\$&') + '$'));
      // Modules that fake mode drops render their HTTP 404 note; either way the page has settled once Loading… is gone.
      await expect(page.locator('main').locator('h2, h3, .card, section, p').first()).toBeVisible();
      await expect(page.locator('main')).not.toContainText('Loading…');
      await expectReflow(page);
      await page.setViewportSize({ width: 375, height: 667 });
      await page.getByRole('button', { name: 'Back', exact: true }).click();
      await expect(page).toHaveURL(/#\/settings$/);
      await expect(page.locator('.version-row')).toBeVisible();
    });
  }
});

selfSizingTest('Chat thread and an existing job reflow at four widths', async ({ signedIn: page }) => {
  await openSeededChat(page);
  await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toBeVisible();
  await expectReflow(page);
  await page.goto('/#/jobs');
  const job = page.locator('.job-row').filter({ hasText: 'Nightly report' }).locator('a.job-main');
  await page.goto('/' + await job.getAttribute('href'));
  await expect(page.locator('#job-name')).toHaveValue('Nightly report');
  await expectReflow(page);
});

test('Rename chat sheet keeps its draft through rotation and closes with Escape, Back and the backdrop', async ({ signedIn: page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const { portrait, landscape } = rotations(page);
  await openSeededChat(page);
  const chatUrl = page.url();
  const sheet = page.getByRole('dialog', { name: 'Rename chat', exact: true });
  const field = sheet.getByRole('textbox', { name: 'Title', exact: true });
  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  await expect(field).toBeFocused();
  await field.fill('Partly typed rename');
  for (const viewport of [landscape, portrait, landscape, portrait]) {
    await page.setViewportSize(viewport);
    await settle(page);
    await expect(sheet).toBeVisible();
    await expect(field).toHaveValue('Partly typed rename');
    await expect(field).toBeFocused();
    for (const name of ['Cancel', 'Rename']) await expect(sheet.getByRole('button', { name, exact: true })).toBeInViewport({ ratio: 1 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveCount(0);
  await expect(page.locator('header h1')).toHaveText('Hello from Chat. What can you do?');

  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  await expect(sheet).toBeVisible();
  await page.goBack();
  await expect(sheet).toHaveCount(0);
  await expect(page).toHaveURL(/#\/chat$/);

  await page.goto(chatUrl);
  await expect(page.locator('.chat-feed')).toContainText('Hi! This is the fake model.');
  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  await expect(sheet).toBeVisible();
  const box = (await sheet.boundingBox())!;
  await page.mouse.click(Math.round(portrait.width / 2), Math.max(4, Math.round(box.y / 2)));
  await expect(sheet).toHaveCount(0);
  await expect(page.locator('header h1')).toHaveText('Hello from Chat. What can you do?');
  expect(errors).toEqual([]);
});

test('Session composer stays usable with the keyboard up in portrait and landscape', async ({ signedIn: page }) => {
  const { portrait, landscape } = rotations(page);
  await openSession(page, 'Tab bar tour');
  await expectDone(page);
  const composer = page.getByPlaceholder('Continue this session…');
  await composer.fill('Typed with the keyboard up');
  await composer.focus();
  for (const viewport of [portrait, landscape]) {
    await page.setViewportSize({ width: viewport.width, height: Math.round(viewport.height * 0.6) });
    await settle(page);
    await expect(composer).toBeFocused();
    await expect(composer).toHaveValue('Typed with the keyboard up');
    await expect(composer).toBeInViewport({ ratio: 1 });
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeInViewport({ ratio: 1 });
    const gap = await composer.evaluate(el => el.getBoundingClientRect().top - document.querySelector('header')!.getBoundingClientRect().bottom);
    expect(gap).toBeGreaterThanOrEqual(0);
  }
});

test('Live resize keeps the Delete job sheet, a partly edited name and focus inside the sheet', async ({ signedIn: page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 900 });
  await settle(page);  // the breakpoint change lands before the navigation, so the list mounts once (#885)
  await page.goto('/#/jobs');
  const job = page.locator('.job-row').filter({ hasText: 'Nightly report' }).locator('a.job-main');
  const jobUrl = '/' + await job.getAttribute('href');
  await page.goto(jobUrl);
  const name = page.locator('#job-name');
  await expect(name).toHaveValue('Nightly report');
  await name.fill('Nightly report partly edited');
  const deleteButton = page.getByRole('button', { name: 'Delete', exact: true });
  // Opened from the keyboard so every engine records the opener; WebKit does not focus a clicked button.
  await deleteButton.focus();
  await page.keyboard.press('Enter');
  const sheet = page.getByRole('dialog', { name: 'Delete the job “Nightly report”?', exact: true });
  await expect(sheet).toBeVisible();
  for (const width of [1280, 1024, 768, 600, 375, 768, 1280, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await settle(page);
    await expect(sheet).toBeVisible();
    await expect(sheet.getByRole('button', { name: 'Cancel', exact: true })).toBeInViewport({ ratio: 1 });
    await expect.poll(() => sheet.evaluate(el => el.contains(document.activeElement))).toBe(true);
    await expect(name).toHaveValue('Nightly report partly edited');
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await sheet.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(sheet).toHaveCount(0);
  await expect(name).toHaveValue('Nightly report partly edited');
  await expect(deleteButton).toBeFocused();
  // Nothing was saved: a reload shows the stored name.
  await page.reload();
  await expect(name).toHaveValue('Nightly report');
  expect(errors).toEqual([]);
});
