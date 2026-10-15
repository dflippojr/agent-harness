import { test, expect, openSession, expectDone, selfSizingTest } from './fixtures';
import type { Locator, Page } from '@playwright/test';

async function expectHitTarget(target: Locator) {
  await target.scrollIntoViewIfNeeded();
  // Use the same normal scrolling/actionability checks as a user click after live resize.
  await target.click({ trial: true });
  await expect.poll(() => target.evaluate(el => {
    const r = el.getBoundingClientRect();
    return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
  })).toBe(true);
}

selfSizingTest('#591 Chat starters and recent chats remain reachable at four reflow widths', async ({ signedIn: page }) => {
  // The welcome paints before options arrive; exercise that startup boundary too.
  await page.route('**/chats/options', async route => {
    await new Promise(resolve => setTimeout(resolve, 500));
    await route.continue();
  });
  for (const width of [320, 375, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/#/chat');
    const starters = page.locator('.chat-starters button');
    await expect(starters.first()).toBeVisible();
    const message = page.getByRole('textbox', { name: 'Message', exact: true });
    await expect(message).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    for (const starter of await starters.all()) {
      await starter.click();
      await expect(message).toHaveValue(await starter.innerText());
    }
    const recent = page.getByRole('region', { name: 'Recent chats', exact: true }).getByRole('link').first();
    await recent.click();
    await expect(page.locator('.chat-feed')).toContainText('Hi! This is the fake model.');
  }
});

test('#603 Chat starter tapped before options load fills the ready composer', async ({ signedIn: page }, info) => {
  let releaseOptions!: () => void;
  const optionsGate = new Promise<void>(resolve => { releaseOptions = resolve; });
  let optionsRequested!: () => void;
  const optionsRequest = new Promise<void>(resolve => { optionsRequested = resolve; });
  await page.route('**/chats/options', async route => {
    optionsRequested();
    await optionsGate;
    await route.continue();
  });
  const prompts: string[] = [];
  page.on('request', request => {
    if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/chats')) {
      prompts.push(request.postDataJSON().prompt);
    }
  });
  try {
    await page.goto('/#/agents');
    await expect(page.locator('.agent-row').first()).toBeVisible();
    await page.goto('/#/chat');
    await optionsRequest;
    const starter = page.locator('.chat-starters button').first();
    await expect(starter).toBeVisible();
    const text = (await starter.innerText()).trim();
    const message = page.getByRole('textbox', { name: 'Message', exact: true });
    await expect(message).toHaveCount(0);
    if (info.project.use.hasTouch) await starter.tap();
    else await starter.click();
    await expect(message).toHaveCount(0);
    releaseOptions();
    await expect(message).toHaveValue(text);
    await expect(message).toBeFocused();
    expect(prompts).toEqual([]);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await expect(page).toHaveURL(new RegExp('#/chat/[A-Za-z0-9_-]+$'));
    expect(prompts).toEqual([text]);
    await expect(page.locator('.msg.assistant.final')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete chat', exact: true }).click();
    await expect(page).toHaveURL(new RegExp('#/chat$'));
  } finally {
    releaseOptions();
  }
});

test('#592 New task labels name and focus their fields', async ({ signedIn: page }) => {
  await page.goto('/#/new');
  for (const label of ['Project', 'Backend', 'Model', 'Title']) {
    const field = page.getByLabel(label, { exact: true });
    await expect(field).toHaveCount(1);
    await page.locator('label').filter({ hasText: new RegExp('^' + label + '$') }).click();
    await expect(field).toBeFocused();
  }
  await page.getByLabel('Project', { exact: true }).selectOption('ui');
  await page.getByLabel('Title', { exact: true }).fill('Partly filled task');
  // Adding a project to the server's catalog is daemon administration (#545): the owner gets the pointer line instead.
  await expect(page.locator('details > summary').filter({ hasText: 'New project' })).toHaveCount(0);
  await expect(page.getByText('Daemon administration is in the Hub or the harness CLI.')).toBeVisible();
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
      // Read all vertical positions together so focus scrolling cannot skew the gaps.
      const gaps = await label.evaluate(el => {
        const toggle = el.getBoundingClientRect();
        const form = el.closest('form')!;
        const above = form.querySelector('#job-notify')!.getBoundingClientRect();
        const footer = form.querySelector('.job-footer')!.getBoundingClientRect();
        return { above: toggle.top - above.bottom, below: footer.top - toggle.bottom };
      });
      expect(gaps.above).toBeGreaterThanOrEqual(8);
      expect(gaps.below).toBeGreaterThanOrEqual(8);
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

selfSizingTest('#593 Tool output actions work through portrait-landscape-portrait without forced taps', async ({ signedIn: page }, info) => {
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

type Box = { left: number; top: number; right: number; bottom: number; width: number; height: number };
const boxOf = (target: Locator) => target.evaluate(el => {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
});
// The clear space between two boxes: horizontal for side-by-side boxes, vertical for stacked ones.
const gapBetween = (a: Box, b: Box) => Math.max(b.left - a.right, a.left - b.right, b.top - a.bottom, a.top - b.bottom);
function expectTargets(boxes: Box[], label: string) {
  for (const [index, box] of boxes.entries()) {
    expect(box.width, `${label} ${index} width`).toBeGreaterThanOrEqual(44);
    expect(box.height, `${label} ${index} height`).toBeGreaterThanOrEqual(44);
    for (const other of boxes.slice(index + 1)) expect(gapBetween(box, other), `${label} ${index} gap`).toBeGreaterThanOrEqual(8);
  }
}
const noPageOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth);
async function openSeededChat(page: Page) {
  await page.goto('/#/chat');
  await page.getByRole('region', { name: 'Recent chats', exact: true })
    .getByRole('link', { name: /^Hello from Chat\. What can you do\?/ }).first().click();
  await expect(page.locator('.chat-feed')).toContainText('Hi! This is the fake model.');
}

test('#626 Actions sub-tabs keep whole-word labels, 44px targets and 8px gaps', async ({ signedIn: page }) => {
  await page.goto('/#/actions/resources');
  const tabs = page.getByRole('tablist', { name: 'Server settings', exact: true });
  await expect(tabs.getByRole('tab', { name: 'Resources', exact: true })).toHaveAttribute('aria-selected', 'true');
  for (const width of [320, 375, 412]) {
    await page.setViewportSize({ width, height: 800 });
    await expect.poll(() => page.evaluate(() => innerWidth)).toBe(width);
    const measured = await page.locator('.resources-tabs button').evaluateAll(els => els.map(el => {
      const r = el.getBoundingClientRect();
      // A word broken across lines has one client rect per line.
      const broken: string[] = [];
      for (const node of el.childNodes) {
        if (node.nodeType !== Node.TEXT_NODE) continue;
        for (const match of node.textContent!.matchAll(/\S+/g)) {
          const range = document.createRange();
          range.setStart(node, match.index!);
          range.setEnd(node, match.index! + match[0].length);
          if (range.getClientRects().length !== 1) broken.push(match[0]);
        }
      }
      const box = { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
      return { box, fits: el.scrollWidth <= el.clientWidth, broken };
    }));
    expect(measured.length).toBeGreaterThanOrEqual(2);
    for (const tab of measured) {
      expect(tab.fits).toBe(true);
      expect(tab.broken).toEqual([]);
    }
    expectTargets(measured.map(tab => tab.box), `tab at ${width}px`);
    await expect.poll(() => noPageOverflow(page)).toBe(true);
  }
  await tabs.getByRole('tab', { name: 'Disk', exact: true }).click();
  await expect(page).toHaveURL(/#\/actions\/disk$/);
});

test('#627 Largest workspaces rows are 44px links 8px apart', async ({ signedIn: page }) => {
  const links = page.locator('.disk-facts p:text-is("Largest workspaces") + ul a');
  for (const width of [375, 412]) {
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/#/actions/disk');
    await expect(links.first()).toBeVisible();
    expectTargets(await Promise.all((await links.all()).map(boxOf)), `workspace at ${width}px`);
    await expect.poll(() => noPageOverflow(page)).toBe(true);
  }
  const href = await links.first().getAttribute('href');
  await links.first().click();
  await expect.poll(() => new URL(page.url()).hash).toBe(href);
});

test('#628 Refresh diagnostics is a 44px target clear of its neighbours', async ({ signedIn: page }) => {
  for (const width of [375, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/#/actions/resources');
    const refresh = page.getByRole('button', { name: 'Refresh diagnostics', exact: true });
    await expect(refresh).toBeVisible();
    const { box, neighbours } = await refresh.evaluate(el => {
      const rect = (node: Element) => {
        const r = node.getBoundingClientRect();
        return { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
      };
      const card = el.closest('.card') || el.parentElement!.parentElement!;
      const others = [...card.querySelectorAll('button, a, input, select, textarea, summary')]
        .filter(other => other !== el && other.getClientRects().length);
      return { box: rect(el), neighbours: others.map(rect) };
    });
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    for (const neighbour of neighbours) expect(gapBetween(box, neighbour)).toBeGreaterThanOrEqual(8);
  }
});

test('#629 Rewind here and Fork from here are 44px targets at least 8px apart', async ({ signedIn: page }) => {
  await openSession(page, 'Tool rows');
  await expectDone(page);
  const checkpoint = page.locator('.note.checkpoint').first();
  for (const width of [320, 375, 412, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await checkpoint.scrollIntoViewIfNeeded();
    const rewind = checkpoint.getByRole('button', { name: 'Rewind here', exact: true });
    const fork = checkpoint.getByRole('button', { name: 'Fork from here', exact: true });
    await expect(rewind).toBeVisible();
    expectTargets([await boxOf(rewind), await boxOf(fork)], `checkpoint action at ${width}px`);
  }
});

test('#630 Cancel the whole task sits at least 8px below the phone decision buttons', async ({ signedIn: page }, info) => {
  test.skip(!info.project.use.isMobile, 'phone approval sheet');
  await openSession(page, 'Protected settings');
  const sheet = page.getByRole('region', { name: 'Approval needed', exact: true });
  await expect(sheet).toBeVisible();
  const cancel = await boxOf(sheet.getByRole('button', { name: 'Cancel the whole task', exact: true }));
  expect(cancel.height).toBeGreaterThanOrEqual(44);
  const decisions = await sheet.locator('.approval-actions button').all();
  expect(decisions).toHaveLength(2);
  for (const button of decisions) {
    const box = await boxOf(button);
    expect(box.height).toBeGreaterThanOrEqual(44);
    expect(cancel.top - box.bottom).toBeGreaterThanOrEqual(8);
  }
});

test('#631 Phone landscape keeps Deny and Approve on screen with transcript above the sheet', async ({ signedIn: page }, info) => {
  test.skip(!info.project.use.isMobile, 'phone approval sheet');
  await openSession(page, 'Protected settings');
  const sheet = page.getByRole('region', { name: 'Approval needed', exact: true });
  await expect(sheet).toBeVisible();
  const viewports = [{ width: 667, height: 375 }, { width: 659, height: 393 }, { width: 320, height: 480 }, { width: 375, height: 667 }];
  for (const viewport of viewports) {
    await page.setViewportSize(viewport);
    await expect.poll(() => page.evaluate(() => innerHeight)).toBe(viewport.height);
    await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
    for (const name of ['✕ Deny', '✓ Approve', 'Cancel the whole task']) {
      const button = sheet.getByRole('button', { name, exact: true });
      const box = await boxOf(button);
      expect(box.top, name).toBeGreaterThanOrEqual(0);
      expect(box.bottom, name).toBeLessThanOrEqual(viewport.height);
      expect(box.left, name).toBeGreaterThanOrEqual(0);
      expect(box.right, name).toBeLessThanOrEqual(viewport.width);
      expect(await button.evaluate(el => {
        const r = el.getBoundingClientRect();
        return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
      }), name).toBe(true);
    }
    // Transcript between the header (the bar, plus the session chrome while it is pinned on screen) and the sheet.
    const band = await page.evaluate(() => {
      const bar = document.getElementById('bar')!.getBoundingClientRect();
      const chrome = document.querySelector('.session-chrome')!.getBoundingClientRect();
      const sheetTop = document.querySelector('.approval-sheet')!.getBoundingClientRect().top;
      return sheetTop - Math.max(bar.bottom, chrome.bottom);
    });
    if (viewport.width > viewport.height) expect(band).toBeGreaterThanOrEqual(80);
    // The height cap must leave that band even when the sheet fills it and sits on a shown tab bar (phone session pages
    // hide the bar today, so show it here).
    const cappedBand = await page.evaluate(() => {
      const tabBar = document.getElementById('tab-bar')!;
      const wasHidden = tabBar.hidden, hadTabs = document.body.classList.contains('has-tabs');
      tabBar.hidden = false;
      document.body.classList.add('has-tabs');
      const sheet = document.querySelector('.approval-sheet')!;
      const band = sheet.getBoundingClientRect().bottom - parseFloat(getComputedStyle(sheet).maxHeight)
        - document.getElementById('bar')!.getBoundingClientRect().bottom;
      tabBar.hidden = wasHidden;
      document.body.classList.toggle('has-tabs', hadTabs);
      return band;
    });
    if (viewport.width > viewport.height) expect(cappedBand).toBeGreaterThanOrEqual(80);
  }
});

test('#632 Chat thread keeps the conversation visible in phone landscape and with the keyboard up', async ({ signedIn: page }, info) => {
  test.skip(!info.project.name.startsWith('iphone-se'), 'iPhone SE geometry');
  await openSeededChat(page);
  const message = page.getByRole('textbox', { name: 'Message', exact: true });
  await message.fill('Partly typed reply');
  await message.focus();
  const portrait = await page.evaluate(() => document.querySelector('.composer')!.getBoundingClientRect().height);
  for (const viewport of [{ width: 667, height: 375 }, { width: 667, height: 225 }]) {
    await page.setViewportSize(viewport);
    await expect.poll(() => page.evaluate(() => innerHeight)).toBe(viewport.height);
    await expect(message).toBeFocused();
    await expect(message).toHaveValue('Partly typed reply');
    await expect.poll(() => page.evaluate(() => {
      const header = document.getElementById('bar')!.getBoundingClientRect();
      const composer = document.querySelector('.composer')!.getBoundingClientRect();
      const feed = document.querySelector('.chat-feed')!.getBoundingClientRect();
      const send = [...document.querySelectorAll('.composer button')].find(b => b.textContent!.trim() === 'Send')!.getBoundingClientRect();
      const field = document.querySelector('.composer textarea')!.getBoundingClientRect();
      const inside = (r: DOMRect) => r.top >= composer.top && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
      return {
        belowHeader: composer.top >= header.bottom,
        controls: inside(send) && inside(field),
        feed: Math.min(feed.bottom, composer.top) - Math.max(feed.top, header.bottom) >= 40,
      };
    })).toEqual({ belowHeader: true, controls: true, feed: true });
    // Pressing Send keeps focus in the field, so the typing layout does not move Send before the release lands.
    const send = page.getByRole('button', { name: 'Send', exact: true });
    const before = await boxOf(send);
    await page.mouse.move(before.left + before.width / 2, before.top + before.height / 2);
    await page.mouse.down();
    await expect(message).toBeFocused();
    expect(await boxOf(send)).toEqual(before);
    await page.mouse.move(1, 1);
    await page.mouse.up();
    await message.focus();
  }
  // In landscape at the latest reply, a growing multi-line draft keeps that reply above the composer.
  await page.setViewportSize({ width: 667, height: 375 });
  await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
  await message.fill(['one', 'two', 'three', 'four'].join('\n'));
  await expect.poll(() => page.evaluate(() => {
    const last = document.querySelector('.chat-feed')!.lastElementChild!.getBoundingClientRect();
    return last.bottom <= document.querySelector('.composer')!.getBoundingClientRect().top;
  })).toBe(true);
  // A keyboard that shrinks only the visual viewport (iOS) lifts the composer; the latest reply still clears it at the end.
  await page.evaluate(() => {
    const vv = visualViewport!;
    Object.defineProperty(vv, 'height', { configurable: true, get: () => innerHeight - 150 });
    vv.dispatchEvent(new Event('resize'));
  });
  await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight));
  await expect.poll(() => page.evaluate(() => {
    const last = document.querySelector('.chat-feed')!.lastElementChild!.getBoundingClientRect();
    return last.bottom <= document.querySelector('.composer')!.getBoundingClientRect().top;
  })).toBe(true);
  await page.evaluate(() => {
    delete (visualViewport as unknown as { height?: number }).height;
    visualViewport!.dispatchEvent(new Event('resize'));
  });
  await message.fill('Partly typed reply');
  await page.setViewportSize({ width: 375, height: 667 });
  await expect.poll(() => page.evaluate(() => document.querySelector('.composer')!.getBoundingClientRect().height)).toBe(portrait);
  await expect(page.locator('.chat-pickers')).toBeVisible();
  await expect(page.locator('#tab-bar')).toBeVisible();
});

test('#633 The page behind an open sheet does not scroll', async ({ signedIn: page }, info) => {
  await openSeededChat(page);
  await page.setViewportSize({ width: info.project.use.viewport!.width, height: 420 });
  await page.evaluate(() => scrollTo(0, 0));
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollHeight > innerHeight)).toBe(true);
  // Mobile WebKit has no wheel events; there the lock is the root's overflow, which also stops touch scrolling.
  const wheel = info.project.name.startsWith('iphone') ? null : async () => {
    await page.mouse.move(info.project.use.viewport!.width / 2, 160);
    await page.mouse.wheel(0, 300);
  };
  const rootOverflow = () => page.evaluate(() => getComputedStyle(document.documentElement).overflowY);
  for (const name of ['Delete', 'Rename']) {
    await page.getByRole('button', { name, exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect.poll(rootOverflow).toBe('hidden');
    if (wheel) {
      await wheel();
      await page.waitForTimeout(300);
      expect(await page.evaluate(() => scrollY)).toBe(0);
    }
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect.poll(rootOverflow).not.toBe('hidden');
    if (wheel) await wheel();
    else await page.evaluate(() => scrollBy(0, 300));
    await expect.poll(() => page.evaluate(() => scrollY)).toBeGreaterThan(0);
    await page.evaluate(() => scrollTo(0, 0));
  }
});

test('#634 Phone jump buttons keep 8px from the session tabs and the Session menu', async ({ signedIn: page }, info) => {
  test.skip(!info.project.use.isMobile, 'phone jump buttons');
  await openSession(page, 'Tool rows');
  await expectDone(page);
  for (const viewport of [{ width: 375, height: 667 }, { width: 667, height: 375 }]) {
    await page.setViewportSize(viewport);
    await page.evaluate(() => scrollTo(0, document.documentElement.scrollHeight / 2));
    const jump = page.locator('.jump:visible').first();
    await expect(jump).toBeVisible();
    const jumpBox = await boxOf(jump);
    const others = [page.getByRole('button', { name: 'Session menu', exact: true }), ...await page.locator('.session-tabs button').all()];
    for (const other of others) expect(gapBetween(jumpBox, await boxOf(other))).toBeGreaterThanOrEqual(8);
  }
});
