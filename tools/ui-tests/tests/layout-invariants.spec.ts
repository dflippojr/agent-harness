// Layout invariants on every phone surface, portrait and landscape (#734). The second UI pass found nine phone layout
// defects (#626-#634) after this suite was green; they are one class, and these checks judge every inventory entry for it:
// - targets: each visible, hittable control is at least 44x44 CSS px, or at least 24x24 (WCAG 2.5.8), and any two are
//   at least 8 px apart;
// - wrapping: no word breaks across lines;
// - sheets: an open sheet's primary actions are inside the visual viewport, and a modal one locks the page behind it;
// - composers: the composer never covers the last message, with or without the virtual keyboard.
import { phoneTest as test, expect } from './fixtures';
import { inventory, type Surface } from './inventory';
import type { Page } from '@playwright/test';

// Each invariant is a soft check with a short settle time, so one run names every violation on a surface.
const check = expect.configure({ soft: true, timeout: 3_000 });

type Rule = 'size' | 'spacing' | 'wrap';
// Reviewed exceptions, each with its reason. `size` and `wrap` entries exempt the elements their selector matches;
// a `spacing` entry exempts a pair only when both match it (rows of one list), so the group stays 8 px from the rest.
const allowList: { rule: Rule; selector: string; reason: string }[] = [
  { rule: 'spacing', selector: '#tab-bar a.tab',
    reason: 'The tab bar is one full-width strip of 44 px tabs that share edges, the platform tab bar pattern.' },
  { rule: 'spacing', selector: 'a.set-row',
    reason: 'Settings rows are a grouped list: full-width 44 px rows divided by a hairline, so a tap lands in one row.' },
  { rule: 'spacing', selector: 'a.agent-row',
    reason: 'Agent rows are full-width list rows divided by a hairline, each far taller than 44 px.' },
  { rule: 'spacing', selector: '.session-menu-item',
    reason: 'Session menu items are full-width 44 px rows of one menu, the platform menu pattern.' },
  { rule: 'wrap', selector: 'pre, code, kbd, textarea, .mono, .tool-preview, .diff',
    reason: 'Code, paths and tool output break long tokens on purpose so they never widen the page.' },
  { rule: 'wrap', selector: '.session-info-value',
    reason: 'Session details are paths and ids, which break anywhere rather than overflow.' },
];
// Known defects of the pinned Stables release (tools/stables/pin.json), each tracked in dflippojr/stables. An entry's
// findings are subtracted on its surfaces and projects, and the case fails once the entry no longer reproduces, so it
// leaves with the re-pin that fixes it.
const SESSION_PAGES = ['Session › Transcript', 'Session › Transcript with tool output', 'Session › Session menu', 'Session › Changes',
  'Session › Info', 'Session › Failed run', 'Session › Approval sheet'];
const knownDefects: { issue: string; surfaces: string[]; projects: string[]; finding: RegExp }[] = [
  { issue: 'dflippojr/stables#1', surfaces: ['Session › Transcript with tool output'],
    projects: ['iphone-se-light', 'pixel7-light', 'phone-landscape'],
    finding: /^spacing (summary "read_file notes\/log\.txt|button\.tool-btn "Copy").* and button\.tool-btn(\.tool-open)? "(Copy|Open)"/ },
  { issue: 'dflippojr/stables#2', surfaces: SESSION_PAGES, projects: ['phone-landscape'],
    finding: /^spacing button(\.on)? "Info" and button\.icon\.session-menu-btn "Session menu"/ },
  { issue: 'dflippojr/stables#2', surfaces: SESSION_PAGES, projects: ['phone-landscape'], finding: /^"(Transcript|Changes)" in button\./ },
  { issue: 'dflippojr/stables#3', surfaces: ['Chat thread'], projects: ['iphone-se-light'],
    finding: /^composer covers the last message by \d+ px with the visual viewport shrunk$/ },
];
const allowed = (rule: Rule) => allowList.filter(entry => entry.rule === rule).map(entry => entry.selector);

const frames = (page: Page) => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));

// The controls a user can see and tap at the current scroll position, with what is wrong with their size and spacing.
// Inside an open modal dialog, only its controls count: the page behind is inert.
function targetViolations(page: Page) {
  return page.evaluate(({ allowSize, spacingGroups }) => {
    const SELECTOR = 'a[href], button, input:not([type=hidden]), select, textarea, summary, label, [role=button], [role=tab],'
      + ' [role=link], [role=menuitem], [role=checkbox], [role=switch], [role=option], [tabindex]:not([tabindex="-1"])';
    const modal = [...document.querySelectorAll('dialog[open]')].find(dialog => dialog.matches(':modal'));
    const root = modal ?? document.body;
    const describe = (el: Element) => {
      const name = (el.getAttribute('aria-label') || (el as HTMLElement).innerText || el.getAttribute('placeholder') || '').trim().replace(/\s+/g, ' ');
      return `${el.tagName.toLowerCase()}${el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : ''} "${name.slice(0, 40)}"`;
    };
    const visible = (el: Element) => {
      if ((el as HTMLButtonElement).disabled || el.closest('[inert]')) return false;
      if (el.tagName === 'LABEL') {
        // A label is its control's target only when it wraps the control (a checkbox row); otherwise the control counts.
        const control = (el as HTMLLabelElement).control;
        if (!control || !el.contains(control)) return false;
      }
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height || getComputedStyle(el).visibility !== 'visible') return false;
      const left = Math.max(r.left, 0), right = Math.min(r.right, innerWidth), top = Math.max(r.top, 0), bottom = Math.min(r.bottom, innerHeight);
      if (right <= left || bottom <= top) return false;
      // Hittable at the centre of its on-screen part: not scrolled under the header, the tab bar or a sheet.
      const hit = document.elementFromPoint((left + right) / 2, (top + bottom) / 2);
      return !!hit && (el.contains(hit) || hit.contains(el));
    };
    // Pinned controls (fixed or sticky chrome: the bar, tab bar, composer, jump buttons) keep their places while the page
    // scrolls under them, so a pinned control and a scrolling one meet only in passing and are not compared. An open menu
    // floats over both and closes on a tap outside it, so its items are compared only with each other.
    const layer = (el: Element) => {
      if (el.closest('[role=menu]')) return 'menu';
      for (let node: Element | null = el; node; node = node.parentElement) {
        if (['fixed', 'sticky'].includes(getComputedStyle(node).position)) return 'pinned';
      }
      return 'page';
    };
    const found = [...root.querySelectorAll(SELECTOR)].filter(visible);
    // A wrapping label is the target for the control inside it.
    const targets = found.filter(el => !found.some(other => other !== el && other.tagName === 'LABEL' && other.contains(el)));
    const boxes = targets.map(el => el.getBoundingClientRect());
    const layers = targets.map(layer);
    const problems: string[] = [];
    const EPS = 0.05;
    targets.forEach((el, i) => {
      const { width, height } = boxes[i];
      if (allowSize.some(selector => el.matches(selector))) return;
      if (width + EPS >= 44 && height + EPS >= 44) return;
      if (width + EPS >= 24 && height + EPS >= 24) return; // undersized but spaced: the spacing check below holds it to 8 px
      problems.push(`size ${describe(el)} is ${Math.round(width)}x${Math.round(height)}`);
    });
    targets.forEach((a, i) => targets.slice(i + 1).forEach((b, k) => {
      if (a.contains(b) || b.contains(a) || layers[i] !== layers[i + 1 + k]) return;
      if (spacingGroups.some(selector => a.matches(selector) && b.matches(selector))) return;
      const r = boxes[i], s = boxes[i + 1 + k];
      const gap = Math.max(s.left - r.right, r.left - s.right, s.top - r.bottom, r.top - s.bottom);
      if (gap + EPS < 8) problems.push(`spacing ${describe(a)} and ${describe(b)} are ${Math.round(gap)} px apart`);
    }));
    return problems;
  }, { allowSize: allowed('size'), spacingGroups: allowed('spacing') });
}

// Words broken across lines. A word is a run of letters and digits, so breaks at hyphens, slashes and spaces are fine.
function brokenWords(page: Page) {
  return page.evaluate(skip => {
    const modal = [...document.querySelectorAll('dialog[open]')].find(dialog => dialog.matches(':modal'));
    const walker = document.createTreeWalker(modal ?? document.body, NodeFilter.SHOW_TEXT);
    const broken: string[] = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || parent.closest(skip) || !parent.getClientRects().length) continue;
      for (const match of node.textContent!.matchAll(/[\p{L}\p{N}]{2,}/gu)) {
        const range = document.createRange();
        range.setStart(node, match.index!);
        range.setEnd(node, match.index! + match[0].length);
        const lines = new Set([...range.getClientRects()].filter(r => r.width > 0).map(r => Math.round(r.bottom)));
        if (lines.size > 1) broken.push(`"${match[0]}" in ${parent.tagName.toLowerCase()}.${[...parent.classList].join('.')}`);
      }
    }
    return broken;
  }, ['script, style, input, select', ...allowed('wrap')].join(', '));
}

// Where the open sheet's primary actions fall outside the visual viewport or cannot be hit.
async function hiddenActions(page: Page, sheet: NonNullable<Surface['sheet']>) {
  const problems: string[] = [];
  for (const name of sheet.actions) {
    const action = page.locator(sheet.selector).getByRole('button', { name, exact: true });
    if (!await action.count()) { problems.push(`${name} is missing`); continue; }
    const problem = await action.first().evaluate(el => {
      const r = el.getBoundingClientRect();
      const vv = visualViewport!;
      const top = vv.offsetTop, left = vv.offsetLeft;
      if (r.top < top - 0.5 || r.left < left - 0.5 || r.bottom > top + vv.height + 0.5 || r.right > left + vv.width + 0.5)
        return `outside the ${Math.round(vv.width)}x${Math.round(vv.height)} visual viewport at ${Math.round(r.left)},${Math.round(r.top)}-${Math.round(r.right)},${Math.round(r.bottom)}`;
      const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
      return hit && el.contains(hit) ? '' : 'covered';
    });
    if (problem) problems.push(`${name} is ${problem}`);
  }
  return problems;
}

// The page behind a modal sheet stays put: the root does not scroll, by overflow or a fixed body. Where the browser has
// wheel events (Chromium), a wheel over the backdrop must also leave the scroll position alone.
async function pageScrollsBehind(page: Page, wheel: boolean) {
  const locked = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement), body = getComputedStyle(document.body);
    return ['hidden', 'clip'].includes(root.overflowY) || ['hidden', 'clip'].includes(body.overflowY) && root.overflowY === 'visible'
      || body.position === 'fixed';
  });
  if (!locked) return 'the root is not locked';
  if (!wheel) return '';
  const before = await page.evaluate(() => scrollY);
  await page.mouse.move(8, 8);
  await page.mouse.wheel(0, 300);
  await frames(page);
  const after = await page.evaluate(() => scrollY);
  return after === before ? '' : `a wheel scrolled the page from ${before} to ${after}`;
}

const scrollTo = (page: Page, where: 'top' | 'middle' | 'bottom') => page.evaluate(where => {
  const max = document.documentElement.scrollHeight - innerHeight;
  window.scrollTo(0, where === 'top' ? 0 : where === 'middle' ? max / 2 : max);
}, where);

// The composer over the last message at the end of the thread, if it covers it.
const coverage = (page: Page, composer: NonNullable<Surface['composer']>, keyboard: string) => page.evaluate(({ field, messages, keyboard }) => {
  window.scrollTo(0, document.documentElement.scrollHeight);
  const box = document.querySelector(field)!.closest('.composer')!.getBoundingClientRect();
  const bottoms = [...document.querySelectorAll(messages)].filter(el => el.getClientRects().length).map(el => el.getBoundingClientRect().bottom);
  const covered = Math.round(Math.max(...bottoms) - box.top);
  return covered > 0 ? [`composer covers the last message by ${covered} px${keyboard}`] : [];
}, { ...composer, keyboard });

for (const surface of inventory) {
  const title = surface.guards ? `${surface.name} (${surface.guards.join(', ')})` : surface.name;
  test(title, async ({ signedIn: page }, info) => {
    const known = knownDefects.filter(entry => entry.surfaces.includes(surface.name) && entry.projects.includes(info.project.name));
    const reproduced = new Set<(typeof known)[number]>();
    // A check's findings less the known defects, noting which of those reproduced.
    const unknown = async (findings: Promise<string[]>) => (await findings).filter(finding => {
      const entry = known.find(candidate => candidate.finding.test(finding));
      if (entry) reproduced.add(entry);
      return !entry;
    });
    await surface.open(page);
    await frames(page);
    // A modal pins the page behind it, so its own controls are measured once; a page is measured top, middle and bottom.
    const positions = surface.sheet?.modal ? ['top'] as const : ['top', 'middle', 'bottom'] as const;
    for (const where of positions) {
      await test.step(`targets at the ${where}`, async () => {
        if (!surface.sheet?.modal) await scrollTo(page, where);
        await check.poll(() => unknown(frames(page).then(() => targetViolations(page))), { message: `targets at the ${where}` }).toEqual([]);
      });
    }
    await test.step('no word breaks across lines', async () => {
      await check.poll(() => unknown(brokenWords(page)), { message: 'words broken across lines' }).toEqual([]);
    });
    if (surface.sheet) {
      const sheet = surface.sheet;
      await test.step('primary actions are on screen', async () => {
        for (const where of positions) {
          if (!sheet.modal) await scrollTo(page, where);
          await check.poll(() => unknown(hiddenActions(page, sheet)), { message: `primary actions at the ${where}` }).toEqual([]);
        }
      });
      if (sheet.modal) await test.step('the page behind does not scroll', async () => {
        check(await pageScrollsBehind(page, info.project.use.defaultBrowserType === 'chromium')).toBe('');
      });
    }
    if (surface.composer) {
      const composer = surface.composer;
      const viewport = page.viewportSize()!;
      await test.step('the composer does not cover the last message', async () => {
        await check.poll(() => unknown(coverage(page, composer, '')), { message: 'composer over the last message' }).toEqual([]);
      });
      await page.locator(composer.field).focus();
      // Android: the keyboard shrinks the layout viewport.
      await test.step('nor with the keyboard shrinking the layout viewport', async () => {
        await page.setViewportSize({ width: viewport.width, height: Math.round(viewport.height * 0.55) });
        await check.poll(() => unknown(coverage(page, composer, ' with the layout viewport shrunk')), { message: 'composer over the last message, layout viewport shrunk' }).toEqual([]);
        await page.setViewportSize(viewport);
      });
      // iOS: the keyboard shrinks only the visual viewport.
      await test.step('nor with the keyboard shrinking only the visual viewport', async () => {
        await page.evaluate(height => {
          Object.defineProperty(visualViewport!, 'height', { configurable: true, get: () => height });
          visualViewport!.dispatchEvent(new Event('resize'));
        }, Math.round(viewport.height * 0.55));
        await check.poll(() => unknown(coverage(page, composer, ' with the visual viewport shrunk')), { message: 'composer over the last message, visual viewport shrunk' }).toEqual([]);
      });
    }
    await test.step('every known defect still reproduces', async () => {
      check(known.filter(entry => !reproduced.has(entry)).map(entry => `${entry.issue} ${entry.finding}`),
        'known defects that no longer reproduce: remove their entries').toEqual([]);
    });
  });
}
