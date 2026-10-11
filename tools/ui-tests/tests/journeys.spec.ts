import { test, expect, openSession, expectDone, createTask, audit } from './fixtures';
import { randomUUID } from 'node:crypto';

test('AH-01/AH-02 connection persists; Agents groups and transcript search', async ({ signedIn: page }) => {
  await page.reload();
  await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
  for (const name of ['Chat', 'Agents', 'Jobs', 'Images', 'Profile']) {
    await expect(page.locator('#tab-bar').getByRole('link', { name: new RegExp(name) })).toBeAttached();
  }
  await page.goto('/#/agents');
  await expect(page.getByRole('region', { name: 'Needs you', exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: 'Search', exact: true }).fill('compaction');
  await expect(page.getByRole('heading', { name: 'Long task with compaction', exact: true })).toBeVisible();
  await page.getByRole('searchbox', { name: 'Search', exact: true }).fill('');
  await page.reload();
  await expect(page.getByRole('region', { name: 'Recent', exact: true })).toBeVisible();
});

test('AH-03 tool output viewer, Changes and Info survive navigation', async ({ signedIn: page }) => {
  await openSession(page, 'Tool rows');
  const row = page.locator('details.tool').filter({ has: page.locator('summary').filter({ hasText: /read_file.*notes\/log.txt/s }) });
  await row.locator('summary').first().click();
  await row.getByRole('button', { name: 'Copy', exact: true }).first().click();
  await expect(page.getByText(/^(Copied|Couldn't reach the clipboard:)/)).toBeVisible();
  // Clipboard denial intentionally opens selected text in the viewer.
  if (await page.getByRole('dialog').count()) await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  // Safari does not focus buttons on pointer activation. Exercise keyboard opening
  // so the viewer has a focused opener to restore, on every browser.
  const opener = row.getByRole('button', { name: 'Open', exact: true }).first();
  await opener.focus();
  await opener.press('Enter');
  const viewer = page.getByRole('dialog');
  await expect(viewer.locator('pre')).toContainText('line 040 the quick brown fox');
  await viewer.getByRole('button', { name: 'Wrap', exact: true }).click();
  await expect(viewer.getByRole('button', { name: 'Wrap', exact: true })).toHaveAttribute('aria-pressed', 'false');
  await page.keyboard.press('Escape');
  await expect(viewer).toHaveCount(0);
  await expect(row.getByRole('button', { name: 'Open', exact: true }).first()).toBeFocused();
  await expect(page.locator('details.tool').filter({ hasText: 'notes/missing.md' }).locator('.tool-state')).toContainText('error');
  await page.getByRole('button', { name: 'Changes', exact: true }).click();
  await expect(page.getByText('No git repositories in this workspace yet.', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Info', exact: true }).click();
  await page.reload();
  await expect(page.locator('.session-tabs button.on')).toHaveText('Info');
});

for (const decision of ['approve', 'deny'] as const) {
  test('AH-04/AH-05 ' + decision + ' persists with matching owner audit', async ({ signedIn: page }) => {
    await createTask(page, 'UI ' + randomUUID(), 'Edit the protected settings file.');
    const sessionId = page.url().split('/').pop()!;
    const sheet = page.getByRole('region', { name: 'Approval needed', exact: true });
    await expect(sheet).toContainText('protected file (fake-model UI testing)');
    await expect(sheet).toContainText('"theme": "system"');
    if (decision === 'deny') {
      await sheet.getByRole('button', { name: 'Add a note for the agent' }).click();
      await sheet.getByPlaceholder('Note for the agent (optional)').fill('UI pass deny');
    }
    await sheet.getByRole('button', { name: decision === 'approve' ? /Approve/ : /Deny/ }).click();
    await expect(sheet).toHaveCount(0);
    await expectDone(page);
    await page.reload();
    await expectDone(page);
    await expect(page.locator('main .approval')).toContainText(decision === 'approve' ? 'approved' : 'denied');
    if (decision === 'deny') await expect(page.locator('main .approval')).toContainText('UI pass deny');
    const handle = await (await page.request.get('/ready')).json();
    await expect.poll(async () => (await audit(page, { action: 'approval.decide' })).filter(row =>
      row.outcome === 'ok' && row.metadata.session_id === sessionId && row.metadata.decision === (decision === 'approve' ? 'approved' : 'denied') &&
      row.target_kind === 'approval' && row.actor_kind === 'owner_key' && row.key_id === handle.key_id
    ).length).toBe(1);
  });
}

test('AH-06 empty validation then markdown task persists', async ({ signedIn: page }) => {
  await page.goto('/#/new');
  await page.getByLabel('Prompt', { exact: true }).fill('');
  await page.getByRole('button', { name: 'Start', exact: true }).click();
  await expect(page.getByText('Write a prompt first', { exact: true })).toBeVisible();
  const title = 'UI markdown ' + randomUUID();
  await createTask(page, title, 'Show me some markdown.');
  await expectDone(page);
  await expect(page.getByRole('heading', { name: 'Summary', exact: true })).toBeVisible();
  await expect(page.locator('main table')).toContainText('Approval sheet');
  await expect(page.locator('main pre')).toContainText('def hello():');
  await page.reload();
  await expectDone(page);
  await expect(page.locator('#title')).toHaveText(title);
});

test('AH-07 running queue and cancellation persist', async ({ signedIn: page }) => {
  const slowUrl = await createTask(page, 'UI slow ' + randomUUID(), 'Run a slow task so I can watch it running.');
  await expect(page.locator('.session-strip .badge').first()).toHaveText('running');
  const queuedUrl = await createTask(page, 'UI queued ' + randomUUID(), 'Hello again');
  await expect(page.locator('.session-strip .badge').first()).toHaveText('queued');
  await page.goto(slowUrl);
  await page.getByRole('button', { name: 'Session menu', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Cancel task', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Cancel task', exact: true }).click();
  await expect(page.locator('.session-strip .badge').first()).toHaveText('cancelled');
  await page.reload();
  await expect(page.locator('.session-strip .badge').first()).toHaveText('cancelled');
  await page.goto(queuedUrl);
  await expectDone(page);
  await page.reload();
  await expectDone(page);
});

test('AH-08/AH-09 retry, fatal rerun and compaction', async ({ signedIn: page }) => {
  await openSession(page, 'Retry after a model error');
  await expect(page.getByText('Model call retried (1)', { exact: true })).toBeVisible();
  await expect(page.locator('main')).toContainText('This reply came from the retry.');
  await openSession(page, 'Failed run');
  await expect(page.locator('main')).toContainText('HTTP 400: simulated fatal model error');
  const oldUrl = page.url();
  await page.getByRole('button', { name: 'Session menu', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Run again as new session' }).click();
  await expect(page).not.toHaveURL(oldUrl);
  await expect(page.locator('.session-strip .badge').first()).toHaveText('failed');
  await page.reload();
  await expect(page.locator('main')).toContainText('HTTP 400: simulated fatal model error');
  await openSession(page, 'Long task with compaction');
  await expectDone(page);
  await expect(page.locator('main .compacting')).toHaveCount(2);
  await page.reload();
  await expect(page.locator('main .compacting')).toHaveCount(2);
});

test('AH-10 rename saves; Escape drops another edit', async ({ signedIn: page }) => {
  await createTask(page, 'UI original ' + randomUUID(), 'Hello');
  await expectDone(page);
  const title = 'UI renamed ' + randomUUID();
  await page.getByRole('button', { name: 'Session menu', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
  await page.getByRole('textbox', { name: 'Session title', exact: true }).fill(title);
  await page.keyboard.press('Enter');
  await expect(page.locator('#title')).toHaveText(title);
  await page.reload();
  await expect(page.locator('#title')).toHaveText(title);
  await page.getByRole('button', { name: 'Session menu', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Rename', exact: true }).click();
  await page.getByRole('textbox', { name: 'Session title', exact: true }).fill('discard');
  await page.keyboard.press('Escape');
  await expect(page.locator('#title')).toHaveText(title);
});

test('AH-12 chat streams, rename persists and delete persists', async ({ signedIn: page }) => {
  await page.getByRole('textbox', { name: 'Message', exact: true }).fill('Hello UI chat');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.locator('.msg.assistant.final')).toContainText('Hi! This is the fake model.');
  await page.reload();
  await expect(page.locator('.msg.assistant.final')).toContainText('Hi! This is the fake model.');
  const title = 'UI chat ' + randomUUID();
  await page.getByRole('button', { name: 'Rename', exact: true }).click();
  const sheet = page.getByRole('dialog', { name: 'Rename chat', exact: true });
  await sheet.getByLabel('Title', { exact: true }).fill(title);
  await sheet.getByRole('button', { name: 'Rename', exact: true }).click();
  await page.reload();
  await expect(page.locator('#title')).toHaveText(title);
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete chat', exact: true }).click();
  await expect(page).toHaveURL(/#\/chat$/);
  await page.reload();
  await expect(page.getByRole('region', { name: 'Recent chats' })).not.toContainText(title);
});

test('AH-13 app theme and connection settings persist', async ({ signedIn: page }) => {
  await page.goto('/#/settings');
  await expect(page.locator('.version-row')).toBeVisible();
  await page.goto('/#/profile/appearance');
  for (const theme of ['Dark', 'Light']) {
    await page.getByRole('button', { name: theme, exact: true }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme.toLowerCase());
    await page.reload();
    await expect(page.getByRole('button', { name: theme, exact: true })).toHaveClass(/on/);
  }
  await page.getByRole('button', { name: 'System', exact: true }).click();
  await page.goto('/#/profile/connection');
  await expect(page.locator('main')).toContainText('http://127.0.0.1:18800');
});

test('AH-14 connection loss and reconnect', async ({ signedIn: page }) => {
  await openSession(page, 'Tool rows');
  await expectDone(page);
  await page.context().setOffline(true);
  await expect(page.locator('#conn')).toHaveAttribute('data-state', 'offline');
  await expect(page.locator('.conn-strip[role=status]')).toBeVisible();
  await page.context().setOffline(false);
  await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
  await expect(page.locator('.conn-strip[role=status]')).toBeHidden();
});
