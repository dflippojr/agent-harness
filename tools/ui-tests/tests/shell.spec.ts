import { test as base, expect as baseExpect, type Page } from '@playwright/test';
import { desktopTest, desktopAndPhoneTest, expect, onlyProjects } from './fixtures';

type Chrome = { title?: string; list?: string; tabs: boolean; tab: string; back: boolean; gear: boolean; add?: string };

// Wide windows (1280px and up) put a list beside its detail, so the list's name replaces the title and Back goes away.
async function expectChrome(page: Page, route: string, want: Chrome) {
  await page.goto('/' + route);
  await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
  if (want.title) await expect(page.locator('#title')).toHaveText(want.title);
  else await expect(page.locator('#title')).toBeHidden();
  if (want.list) await expect(page.locator('#split-list')).toHaveAttribute('aria-label', want.list);
  else await expect(page.locator('body')).not.toHaveClass(/(^| )split( |$)/);
  await expect(page.locator('#tab-bar')).toBeVisible({ visible: want.tabs });
  await expect(page.locator('#tab-bar a[aria-current=page]')).toHaveAttribute('data-tab', want.tab);
  await expect(page.locator('#back')).toBeVisible({ visible: want.back });
  await expect(page.locator('#settings-btn')).toBeVisible({ visible: want.gear });
  // A wide window shows the list's New action in its header; a phone shows the floating button with the same label.
  const wide = (page.viewportSize()?.width ?? 0) >= 1280;
  const add = page.locator(wide ? '.list-new' : '#fab');
  if (want.add) await expect(add).toHaveText(want.add);
  else await expect(add).toBeHidden();
  if (want.add && !wide) await expect(page.locator('.list-new')).toBeHidden();
  await expect.poll(() => page.locator('main').innerText()).not.toMatch(/(^|\s)null(\s|$)/);
}

desktopAndPhoneTest('AH-17 shell chrome per route', async ({ signedIn: page }) => {
  const requests: string[] = [];
  page.on('request', request => requests.push(request.url()));
  await page.goto('/#/jobs');
  const job = await page.locator('.job-row', { hasText: 'Nightly report' }).first().getAttribute('data-job');
  await page.goto('/#/agents');
  const session = (await page.locator('a.agent-row', { hasText: 'Tab bar tour' }).first().getAttribute('href'))!;
  const wide = (page.viewportSize()?.width ?? 0) >= 1280;
  const routes: [string, Chrome][] = [
    ['#/chat', { title: 'Chat', tabs: true, tab: 'chat', back: false, gear: true }],
    ['#/agents', { title: wide ? undefined : 'Agents', list: wide ? 'Agents list' : undefined, tabs: true, tab: 'agents', back: false, gear: true, add: '+ New task' }],
    ['#/jobs', { title: wide ? undefined : 'Jobs', list: wide ? 'Jobs list' : undefined, tabs: true, tab: 'jobs', back: false, gear: true, add: '+ New job' }],
    ['#/images', { title: 'Images', tabs: true, tab: 'images', back: false, gear: true }],
    ['#/profile', { title: wide ? undefined : 'Profile', list: wide ? 'Settings menu' : undefined, tabs: true, tab: 'profile', back: false, gear: true }],
    ['#/settings', { title: wide ? undefined : 'Settings', list: wide ? 'Settings menu' : undefined, tabs: true, tab: 'profile', back: !wide, gear: false }],
    ['#/profile/appearance', { title: 'Appearance', list: wide ? 'Settings menu' : undefined, tabs: true, tab: 'profile', back: !wide, gear: false }],
    ['#/actions/resources', { title: 'Actions', list: wide ? 'Settings menu' : undefined, tabs: true, tab: 'profile', back: !wide, gear: false }],
    ['#/new', { title: 'New task', tabs: wide, tab: 'agents', back: true, gear: false }],
    ['#/jobs/' + job, { title: 'Job', list: wide ? 'Jobs list' : undefined, tabs: wide, tab: 'jobs', back: !wide, gear: false, add: wide ? '+ New job' : undefined }],
    [session, { title: 'Tab bar tour', list: wide ? 'Agents list' : undefined, tabs: wide, tab: 'agents', back: !wide, gear: false, add: wide ? '+ New task' : undefined }],
  ];
  for (const [route, want] of routes) await base.step(route, () => expectChrome(page, route, want));
  // The Actions tabs, and the live count of agents that need the owner.
  await page.goto('/#/actions/resources');
  await expect(page.getByRole('tab')).toHaveText(['Resources', 'Claude Remote Control', 'Disk']);
  await page.goto('/#/agents');
  const needs = page.getByRole('region', { name: 'Needs you', exact: true }).locator('a.agent-row');
  await expect(needs.first()).toBeVisible();
  const count = page.locator('#agents-needs-you');
  if (wide) await expect(count).toHaveText(String(await needs.count()));
  else await expect(count).toBeHidden();
  // Old bookmarks land on today's pages.
  for (const [from, to] of [
    ['#/tasks', '#/jobs'], ['#/profile/disk', '#/actions/disk'], ['#/settings/disk', '#/actions/disk'],
    ['#/actions', '#/actions/resources'], ['#/actions/gpu', '#/actions/resources'],
    ['#/profile/accounts', '#/profile'], ['#/profile/daemon', '#/profile'],
  ]) {
    await page.goto('/' + from);
    await expect(page).toHaveURL(url => url.hash === to);
  }
  // An id that could climb out of its route is never sent to the server.
  requests.length = 0;
  for (const hostile of ['#/s/..%2Fx', '#/chat/a%5Cb']) {
    await page.goto('/' + hostile);
    await expect(page.locator('#conn')).toHaveAttribute('data-state', 'live');
    await expect(page.locator('main')).not.toBeEmpty();
  }
  expect(requests.filter(url => url.includes('..') || url.includes('%5C'))).toEqual([]);
});

desktopTest('AH-18 desktop split panes', async ({ signedIn: page }) => {
  const pane = page.locator('#split-list');
  const toggle = page.getByRole('button', { name: 'Hide the list', exact: true });
  const wideAgain = async () => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(page.locator('body')).toHaveClass(/(^| )split( |$)/);
  };
  await base.step('Agents: the open row is marked and the list pane is kept', async () => {
    await page.goto('/#/agents');
    const rows = pane.locator('a.agent-row');
    await expect(rows.first()).toBeVisible();
    await expect(page.getByText('No session open', { exact: true })).toBeVisible();
    await pane.locator('.split-body').evaluate(el => { (el as HTMLElement).dataset.marker = 'kept'; });
    await rows.filter({ hasText: 'Tab bar tour' }).first().click();
    await expect(page.locator('#title')).toHaveText('Tab bar tour');
    await expect(pane.locator('a.agent-row[aria-current=page]')).toHaveCount(1);
    await expect(pane.locator('a.agent-row[aria-current=page]')).toContainText('Tab bar tour');
    await rows.filter({ hasText: 'Tool rows' }).first().click();
    await expect(page.locator('#title')).toHaveText('Tool rows');
    await expect(pane.locator('a.agent-row[aria-current=page]')).toContainText('Tool rows');
    await expect(pane.locator('.split-body')).toHaveAttribute('data-marker', 'kept');
  });
  await base.step('Agents: [ hides the list, but not while typing', async () => {
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await page.keyboard.press('[');
    await expect(page.locator('body')).toHaveClass(/split-collapsed/);
    await expect(toggle).toHaveAttribute('aria-pressed', 'true');
    await page.keyboard.press('[');
    await expect(page.locator('body')).not.toHaveClass(/split-collapsed/);
    const composer = page.getByPlaceholder('Continue this session…');
    await composer.click();
    await page.keyboard.type('[ is text');
    await expect(composer).toHaveValue('[ is text');
    await expect(page.locator('body')).not.toHaveClass(/split-collapsed/);
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  });
  await base.step('Agents: narrowing keeps the draft and shows Back', async () => {
    const composer = page.getByPlaceholder('Continue this session…');
    await expect(page.locator('#back')).toBeHidden();
    await page.setViewportSize({ width: 1024, height: 900 });
    await expect(composer).toHaveValue('[ is text');
    await expect(page.locator('#back')).toBeVisible();
    await expect(page.locator('body')).not.toHaveClass(/(^| )split( |$)/);
    await wideAgain();
    await expect(composer).toHaveValue('[ is text');
    await expect(page.locator('#back')).toBeHidden();
  });
  await base.step('Agents: a list that cannot load says so, and Retry restores it', async () => {
    const down = (url: URL) => url.pathname.endsWith('/v1/sessions');
    await page.route(down, route => route.request().method() === 'GET'
      ? route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'bad gateway' }) }) : route.fallback());
    await page.goto('/#/agents');
    await page.reload();
    await expect(pane.locator('.note.bad')).toBeVisible();
    await expect(pane.locator('a.agent-row')).toHaveCount(0);
    await page.unroute(down);
    await pane.getByRole('button', { name: 'Retry', exact: true }).click();
    await expect(pane.locator('a.agent-row').first()).toBeVisible();
    await expect(pane.locator('.note.bad')).toHaveCount(0);
  });
  await base.step('Jobs: an unsaved name survives narrowing and widening', async () => {
    await page.goto('/#/jobs');
    const job = await pane.locator('.job-row', { hasText: 'Nightly report' }).first().getAttribute('data-job');
    await page.goto('/#/jobs/' + job);
    const name = page.locator('#job-name');
    await expect(name).toHaveValue('Nightly report');
    await name.fill('Unsaved job name');
    await page.setViewportSize({ width: 1024, height: 900 });
    await expect(name).toHaveValue('Unsaved job name');
    await expect(page.locator('#back')).toBeVisible();
    await wideAgain();
    await expect(name).toHaveValue('Unsaved job name');
    await expect(pane.locator('.job-row[aria-current=page], [aria-current=page] .job-row').first()).toBeVisible();
  });
  await base.step('Settings: the menu names an empty detail, follows a change and keeps an unsaved URL', async () => {
    await page.goto('/#/settings');
    await expect(pane).toHaveAttribute('aria-label', 'Settings menu');
    await expect(page.getByText('No setting open', { exact: true })).toBeVisible();
    const appearance = pane.locator('a.set-row[data-setting=appearance]');
    await appearance.click();
    await page.getByRole('button', { name: 'Dark', exact: true }).click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(appearance).toContainText('Dark');
    await expect(appearance).toHaveAttribute('aria-current', 'page');
    await pane.locator('a.set-row[data-setting=connection]').click();
    const url = page.locator('main input').first();
    await url.fill('https://unsaved.example');
    await page.setViewportSize({ width: 1024, height: 900 });
    await expect(url).toHaveValue('https://unsaved.example');
    await expect(page.locator('#back')).toBeVisible();
    await wideAgain();
    await expect(url).toHaveValue('https://unsaved.example');
  });
});

desktopTest('AH-19 keyboard shortcuts', async ({ signedIn: page }) => {
  await page.goto('/#/agents');
  const rows = page.locator('#split-list a.agent-row');
  await expect(rows.first()).toBeVisible();
  const search = page.getByRole('searchbox', { name: 'Search', exact: true });
  await search.focus();
  await page.keyboard.press('Escape');
  await search.evaluate(el => (el as HTMLElement).blur());
  // ? opens the list with Close focused, and Escape closes it and gives focus back.
  await rows.first().focus();
  await page.keyboard.press('Shift+?');
  const dialog = page.getByRole('dialog', { name: 'Keyboard shortcuts', exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Close', exact: true })).toBeFocused();
  await expect(dialog).toContainText('Approve and Deny have no single-key shortcut');
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(rows.first()).toBeFocused();
  // / focuses search.
  await page.keyboard.press('/');
  await expect(search).toBeFocused();
  // Typing in a field never triggers a shortcut.
  await page.keyboard.press('n');
  await expect(search).toHaveValue('n');
  await expect(page).toHaveURL(/#\/agents$/);
  await search.fill('');
  await search.evaluate(el => (el as HTMLElement).blur());
  // The cleared search shows the list again only after its 250 ms debounce; j would otherwise focus a search result.
  await expect(rows.first()).toBeVisible();
  // j j k walks the rows, g j goes to Jobs, n starts a new task.
  await page.keyboard.press('j');
  await expect(rows.nth(0)).toBeFocused();
  await page.keyboard.press('j');
  await expect(rows.nth(1)).toBeFocused();
  await page.keyboard.press('k');
  await expect(rows.nth(0)).toBeFocused();
  await page.keyboard.press('n');
  await expect(page).toHaveURL(/#\/new$/);
  await page.keyboard.press('g');
  await page.keyboard.press('j');
  await expect(page).toHaveURL(/#\/jobs$/);
});

base('AH-20 protocol skew blocks the app', async ({ page }, info) => {
  onlyProjects(info, ['desktop-light']);
  const api: string[] = [];
  page.on('request', request => {
    if (/\/api\/(admin\/)?v1\//.test(request.url())) api.push(request.method() + ' ' + new URL(request.url()).pathname);
  });
  // The browser app is too old for the server, then the server is too old for the browser app (which has nothing to reload).
  for (const { admin, heading, button } of [
    { admin: { min: 99, max: 100 }, heading: 'Update Stables', button: true },
    { admin: { min: 0, max: 0 }, heading: 'Update Agent Harness Server', button: false },
  ]) {
    api.length = 0;
    await page.route('**/health', async route => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({ response, json: { ...body, protocols: { ...body.protocols, admin } } });
    });
    await page.goto('/connect');
    const reload = page.getByRole('button', { name: 'Reload and update', exact: true });
    const card = page.getByRole('heading', { name: heading, exact: true });
    const expectCard = async () => {
      await baseExpect(card).toBeVisible();
      await baseExpect(reload).toHaveCount(button ? 1 : 0);
    };
    await expectCard();
    await baseExpect(page.locator('#tab-bar')).toBeHidden();
    // Boot asks /health and /me together, so one /me may already be in flight when the card goes up. Nothing follows it.
    const atCard = [...api];
    baseExpect(atCard.filter(call => call !== 'GET /api/admin/v1/me')).toEqual([]);
    await page.goto('/#/profile');
    await page.evaluate(() => {
      window.dispatchEvent(new Event('online'));
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await expectCard();
    await baseExpect(page.locator('#tab-bar')).toBeHidden();
    await baseExpect(page.locator('#conn')).toBeHidden();
    baseExpect(api).toEqual(atCard);
    await page.unroute('**/health');
  }
});
