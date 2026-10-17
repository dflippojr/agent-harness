// Every phone surface of Stables on the fake-model instance (#734): each page, sub-tab, sheet and dialog, and how to
// reach it from a signed-in page. layout-invariants.spec.ts runs each entry on the phone projects. `guards` names the
// defects whose class an entry covers, so a regression of #626-#634 fails a named case.
import { expect, type Page } from '@playwright/test';
import { openSession, expectDone } from './fixtures';

export type Surface = {
  name: string;
  guards?: string[];
  open: (page: Page) => Promise<void>;
  // An open sheet or dialog: the accessible names of its primary actions. A modal one also locks the page behind it.
  sheet?: { selector: string; actions: string[]; modal: boolean };
  // A composer that must not cover the last message, also with the virtual keyboard up. `messages` selects the thread's
  // messages; the lowest one is the last.
  composer?: { field: string; messages: string };
};

async function goto(page: Page, route: string, ready: string) {
  await page.goto(route);
  await expect(page.locator(ready).first()).toBeVisible();
}
async function openChat(page: Page) {
  await page.goto('/#/chat');
  // Follow the row's link rather than tap it: reaching the thread is the point here, not the landing page's layout.
  const href = await page.getByRole('region', { name: 'Recent chats', exact: true })
    .getByRole('link', { name: /^Hello from Chat\. What can you do\?/ }).first().getAttribute('href');
  await page.goto('/' + href);
  await expect(page.locator('.chat-feed')).toContainText('Hi! This is the fake model.');
}
async function openDone(page: Page, title: string, tab = '') {
  await openSession(page, title);
  await expectDone(page);
  if (tab) {
    await page.locator('.session-tabs').getByRole('button', { name: tab, exact: true }).click();
    await expect(page.locator('.session-tabs button.on')).toHaveText(tab);
  }
}
async function openSheetFrom(page: Page, button: string, title: string) {
  await page.getByRole('button', { name: button, exact: true }).first().click();
  await expect(page.getByRole('dialog').filter({ hasText: title })).toBeVisible();
}
const SHEET = 'dialog.sheet[open]';
const CHAT_COMPOSER = { field: '.chat-composer textarea', messages: '.chat-feed > *' };
const SESSION_COMPOSER = { field: '.composer textarea', messages: 'main .ev' };

const settingsPages = ['appearance', 'notifications', 'connection', 'install', 'backends', 'smart-approvals', 'memory', 'endpoint', 'account']
  .map(section => ({ name: `Settings › ${section}`, open: (page: Page) => goto(page, `/#/profile/${section}`, 'main h2, main .card') }));

export const inventory: Surface[] = [
  { name: 'Chat landing', open: page => goto(page, '/#/chat', '.chat-welcome') },
  { name: 'Chat thread', guards: ['#632'], composer: CHAT_COMPOSER, open: openChat },
  { name: 'Chat thread › Rename sheet', guards: ['#633'], sheet: { selector: SHEET, actions: ['Cancel', 'Rename'], modal: true },
    open: async page => { await openChat(page); await openSheetFrom(page, 'Rename', 'Rename'); } },
  { name: 'Chat thread › Delete sheet', guards: ['#633'], sheet: { selector: SHEET, actions: ['Cancel', 'Delete chat'], modal: true },
    open: async page => { await openChat(page); await openSheetFrom(page, 'Delete', 'Delete'); } },
  { name: 'Agents', open: page => goto(page, '/#/agents', '.agent-row') },
  { name: 'New task', open: page => goto(page, '/#/new', '.new-task-page') },
  { name: 'Jobs', open: page => goto(page, '/#/jobs', '.job-row') },
  { name: 'Job form', open: page => goto(page, '/#/jobs/new', '#job-name') },
  { name: 'Job edit', open: async page => {
    await goto(page, '/#/jobs', '.job-row');
    await page.locator('.job-row').filter({ hasText: 'Weekly cleanup' }).locator('a.job-main').click();
    await expect(page.locator('#job-name')).toBeVisible();
  } },
  { name: 'Profile', open: page => goto(page, '/#/profile', 'a.card.identity') },
  { name: 'Settings', open: page => goto(page, '/#/settings', '.version-row') },
  ...settingsPages,
  { name: 'Actions › Resources', guards: ['#626', '#628'], open: async page => {
    await goto(page, '/#/actions/resources', '.resources-tabs');
    await expect(page.getByRole('button', { name: 'Refresh diagnostics', exact: true })).toBeVisible();
  } },
  { name: 'Actions › Claude Remote Control', guards: ['#626'], open: page => goto(page, '/#/actions/remote-control', '.resources-tabs') },
  { name: 'Actions › Disk', guards: ['#626', '#627'], open: page => goto(page, '/#/actions/disk', '.disk-facts') },
  { name: 'Session › Transcript', guards: ['#629', '#634'], composer: SESSION_COMPOSER, open: page => openDone(page, 'Tool rows') },
  { name: 'Session › Transcript with tool output', guards: ['#629'], open: async page => {
    await openDone(page, 'Tool rows');
    await page.locator('details.tool summary').filter({ hasText: /read_file.*notes\/log.txt/s }).first().click();
  } },
  { name: 'Session › Tool output viewer', sheet: { selector: 'dialog[open]', actions: ['Close'], modal: true }, open: async page => {
    await openDone(page, 'Tool rows');
    const row = page.locator('details.tool').filter({ has: page.locator('summary').filter({ hasText: /read_file.*notes\/log.txt/s }) });
    await row.locator('summary').first().click();
    await row.getByRole('button', { name: 'Open', exact: true }).first().click();
    await expect(page.getByRole('dialog').locator('pre')).toContainText('line 040');
  } },
  { name: 'Session › Rewind sheet', guards: ['#633'], sheet: { selector: SHEET, actions: ['Cancel', 'Rewind'], modal: true }, open: async page => {
    await openDone(page, 'Tool rows');
    await page.locator('.note.checkpoint').first().getByRole('button', { name: 'Rewind here', exact: true }).click();
    await expect(page.locator(SHEET)).toBeVisible();
  } },
  { name: 'Session › Session menu', guards: ['#634'], open: async page => {
    await openDone(page, 'Tool rows');
    await page.getByRole('button', { name: 'Session menu', exact: true }).click();
    await expect(page.getByRole('menu', { name: 'Session', exact: true })).toBeVisible();
  } },
  { name: 'Session › Changes', open: page => openDone(page, 'Tool rows', 'Changes') },
  { name: 'Session › Info', open: page => openDone(page, 'Tool rows', 'Info') },
  { name: 'Session › Failed run', composer: SESSION_COMPOSER, open: async page => {
    await openSession(page, 'Failed run');
    await expect(page.locator('.session-strip .badge').first()).toHaveText('failed');
  } },
  { name: 'Session › Approval sheet', guards: ['#630', '#631'],
    // Deny and Approve decide; Cancel the whole task is secondary and leaves the sheet on the wide layout (phone landscape).
    sheet: { selector: '.approval-sheet', actions: ['✕ Deny', '✓ Approve'], modal: false },
    open: async page => {
      await openSession(page, 'Protected settings');
      await expect(page.getByRole('region', { name: 'Approval needed', exact: true })).toBeVisible();
    } },
];
