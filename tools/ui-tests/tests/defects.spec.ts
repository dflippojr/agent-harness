import { test, expect, openSession, expectDone } from './fixtures';
import type { Locator } from '@playwright/test';

async function expectHitTarget(target: Locator) {
  await target.scrollIntoViewIfNeeded();
  // Use the same normal scrolling/actionability checks as a user click after live resize.
  await target.click({ trial: true });
  await expect.poll(() => target.evaluate(el => {
    const r = el.getBoundingClientRect();
    return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
  })).toBe(true);
}

test('#591 Chat starters and recent chats remain reachable at four reflow widths', async ({ signedIn: page }) => {
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/#/chat');
    const starters = page.locator('.chat-starters button');
    await expect(starters.first()).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const starter of await starters.all()) {
      await starter.click();
      await expect(page.getByRole('textbox', { name: 'Message', exact: true })).toHaveValue(await starter.innerText());
    }
    const recent = page.getByRole('region', { name: 'Recent chats', exact: true }).getByRole('link').first();
    await recent.click();
    await expect(page.locator('.chat-feed')).toContainText('Hi! This is the fake model.');
  }
});

test('#592 New task and optional project labels name and focus their fields', async ({ signedIn: page }) => {
  await page.goto('/#/new');
  for (const label of ['Project', 'Backend', 'Model', 'Title']) {
    const field = page.getByLabel(label, { exact: true });
    await expect(field).toHaveCount(1);
    await page.locator('label').filter({ hasText: new RegExp('^' + label + '$') }).click();
    await expect(field).toBeFocused();
  }
  await page.getByLabel('Project', { exact: true }).selectOption('ui');
  await page.getByLabel('Title', { exact: true }).fill('Partly filled task');
  await page.locator('details > summary').filter({ hasText: 'New project' }).click();
  for (const label of ['Name', 'Description', 'Runs on', 'Workspace']) {
    const field = page.getByLabel(label, { exact: true });
    await expect(field).toHaveCount(1);
    await page.locator('label').filter({ hasText: new RegExp('^' + label + '$') }).click();
    await expect(field).toBeFocused();
  }
  await page.getByLabel('Workspace', { exact: true }).selectOption('repo');
  await expect(page.getByLabel('Repository', { exact: true })).toBeVisible();
  await expect(page.getByLabel('Project', { exact: true })).toHaveValue('ui');
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('Partly filled task');
});

test('#594 Enabled label has a 44px target with separated actions in create and edit forms', async ({ signedIn: page }, info) => {
  await page.goto('/#/jobs');
  const existing = await page.locator('.job-row').filter({ hasText: 'Weekly cleanup' }).locator('a.job-main').getAttribute('href');
  for (const route of ['/#/jobs/new', '/' + existing]) {
    await page.goto(route);
    await expect(page.getByRole('region', { name: route.endsWith('/new') ? 'New job' : 'Weekly cleanup', exact: true })).toBeVisible();
    const label = page.locator('label.job-enabled');
    const checkbox = page.getByRole('checkbox', { name: 'Job enabled', exact: true });
    for (const width of [375, 412]) {
      await page.setViewportSize({ width, height: 915 });
      await label.scrollIntoViewIfNeeded();
      const box = (await label.boundingBox())!;
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
      const footer = (await page.locator('.job-footer').boundingBox())!;
      expect(footer.y - box.y - box.height).toBeGreaterThanOrEqual(8);
      for (const position of [{ x: 2, y: 2 }, { x: box.width - 2, y: box.height - 2 }]) {
        const checked = await checkbox.isChecked();
        if (info.project.use.hasTouch) await label.tap({ position });
        else await label.click({ position });
        await expect(checkbox).toBeChecked({ checked: !checked });
      }
      await checkbox.focus();
      const checked = await checkbox.isChecked();
      await checkbox.press('Space');
      await expect(checkbox).toBeChecked({ checked: !checked });
    }
  }
});

test('#595 Session tabs retain 44px targets, 8px gaps and working labels at narrow widths', async ({ signedIn: page }) => {
  await openSession(page, 'Tool rows');
  await expectDone(page);
  for (const width of [320, 375, 412]) {
    await page.setViewportSize({ width, height: 915 });
    const buttons = page.locator('.session-tabs button');
    const boxes = await buttons.evaluateAll(els => els.map(el => {
      const r = el.getBoundingClientRect();
      return { x: r.x, width: r.width, height: r.height, fits: el.scrollWidth <= el.clientWidth };
    }));
    expect(boxes).toHaveLength(3);
    for (const [index, box] of boxes.entries()) {
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
      expect(box.fits).toBe(true);
      if (index) expect(box.x - boxes[index - 1].x - boxes[index - 1].width).toBeGreaterThanOrEqual(8);
    }
    for (const name of ['Changes', 'Info', 'Transcript']) {
      await page.locator('.session-tabs').getByRole('button', { name, exact: true }).click();
      await expect(page.locator('.session-tabs button.on')).toHaveText(name);
    }
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});

test('#593 Tool output actions work through portrait-landscape-portrait without forced taps', async ({ signedIn: page }, info) => {
  await openSession(page, 'Tool rows');
  await expectDone(page);
  const row = page.locator('details.tool').filter({ has: page.locator('summary').filter({ hasText: /read_file.*notes\/log.txt/s }) });
  await row.locator('summary').first().click();
  for (const viewport of [{ width: 375, height: 667 }, { width: 667, height: 375 }, { width: 375, height: 667 }]) {
    await page.setViewportSize(viewport);
    for (const name of ['Copy', 'Open']) {
      if (name === 'Copy') await expect(page.locator('#toast')).toBeHidden();
      const action = row.getByRole('button', { name, exact: true }).first();
      await expectHitTarget(action);
      if (info.project.use.hasTouch) await action.tap();
      else await action.click();
      if (name === 'Copy') await expect(page.getByText(/^(Copied|Couldn't reach the clipboard:)/)).toBeVisible();
      const viewer = page.getByRole('dialog');
      if (name === 'Open') await expect(viewer.locator('pre')).toContainText('line 040 the quick brown fox');
      if (await viewer.count()) {
        const close = viewer.getByRole('button', { name: 'Close', exact: true });
        await expectHitTarget(close);
        if (info.project.use.hasTouch) await close.tap();
        else await close.click();
        await expect(viewer).toHaveCount(0);
      }
    }
  }
});
