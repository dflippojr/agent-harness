import { desktopTest as test, expect, createTask, openSession } from './fixtures';
import { randomUUID } from 'node:crypto';

test('AH-21 superseded approval, cancel while pending, transcript download', async ({ signedIn: page }) => {
  await createTask(page, 'UI two approvals ' + randomUUID(), 'Please run two approvals in one turn.');
  const sessionId = page.url().split('/').pop()!;
  const sheet = page.getByRole('region', { name: 'Approval needed', exact: true });
  await expect(sheet).toContainText('a.json');
  await sheet.getByRole('button', { name: /Approve/ }).click();
  // The second write asks in the same turn: its sheet replaces the first one.
  await expect(sheet).toContainText('b.json');
  await expect(sheet).not.toContainText('a.json');
  await page.getByRole('button', { name: 'Session menu', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Cancel task', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel task', exact: true }).click();
  await expect(sheet).toHaveCount(0);
  await expect(page.locator('.session-strip .badge').first()).toHaveText('cancelled');
  await page.getByRole('button', { name: 'Session menu', exact: true }).click();
  const download = page.waitForEvent('download');
  await page.getByRole('menuitem', { name: 'Download transcript', exact: true }).click();
  expect((await download).suggestedFilename()).toBe(sessionId + '.md');
});

test('AH-22 desktop session pane approval bar', async ({ signedIn: page }) => {
  await openSession(page, 'Protected settings');
  const sheet = page.getByRole('region', { name: 'Approval needed', exact: true });
  await expect(sheet).toBeVisible();
  const sessionUrl = page.url();
  // The status strip and the Transcript | Changes | Info control share the bar on desktop and sit under it on a phone.
  await expect(page.locator('#bar .session-strip')).toBeVisible();
  await expect(page.locator('#bar .session-tabs')).toBeVisible();
  await expect(page.locator('.session-chrome .session-strip, .session-chrome .session-tabs')).toHaveCount(0);
  // An arriving approval never takes focus to Approve.
  await expect(page.getByRole('button', { name: /Approve/ })).not.toBeFocused();
  await page.setViewportSize({ width: 375, height: 667 });
  await expect(page.locator('.session-chrome .session-strip')).toBeVisible();
  await expect(page.locator('.session-chrome .session-tabs')).toBeVisible();
  await expect(page.locator('#bar .session-strip, #bar .session-tabs')).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 900 });
  await expect(page.locator('#bar .session-tabs')).toBeVisible();
  for (const tab of ['Changes', 'Info']) {
    await page.getByRole('button', { name: tab, exact: true }).click();
    const bar = page.locator('.approval-bar[role=status]');
    await expect(bar).toContainText('Approval needed · protected file');
    await bar.getByRole('link', { name: 'Review' }).click();
    await expect(sheet).toBeVisible();
    await expect(page).toHaveURL(new RegExp('#/s/[^/]+(/approval/[^/]+)?$'));
    await page.goto(sessionUrl);
  }
});
