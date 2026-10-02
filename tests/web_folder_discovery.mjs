import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mountActions } from '../harness/web/pages/actions.mjs';
import { settingInput } from '../harness/web/lib/setting-input.mjs';

const css = readFileSync(new URL('../harness/web/style.css', import.meta.url), 'utf8');

class Element {
  constructor(tag, attrs, children) {
    this.tag = tag;
    Object.assign(this, attrs);
    this.children = children.flat(Infinity).filter(value => value != null);
    this.listeners = {};
  }
  addEventListener(name, listener) { this.listeners[name] = listener; }
}
const h = (tag, attrs, ...children) => new Element(tag, attrs, children);
const text = node => node instanceof Element ? node.children.map(text).join(' ') : String(node ?? '');
const find = (node, predicate) => {
  if (!(node instanceof Element)) return null;
  if (predicate(node)) return node;
  for (const child of node.children) { const result = find(child, predicate); if (result) return result; }
  return null;
};
const button = (node, label) => find(node, n => n.tag === 'button' && text(n) === label);
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const limits = { visited_directories: 20000, candidates: 500, seconds: 30, errors: 50, expiry_seconds: 900 };
const candidate = { id: 'opaque', path: 'C:\\Projects\\Sample', suggested_slug: 'sample', markers: ['package.json'],
  trusted: false, requires_git: false, launchable: true, promoted: false, configured_duplicate: false };

for (const width of [390, 1440]) {
  const calls = [], confirms = [], timers = [], leave = [];
  let supported = true, enabled = false, reloads = 0, projects = [];
  let scan = { id: 'scan', status: 'running', visited: 10, candidates: [], errors: [], expires_in: 900, truncated: false };
  globalThis.setTimeout = (fn, ms) => { timers.push({fn, ms}); return timers.length; };
  globalThis.clearTimeout = () => {}; globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
  globalThis.window = { innerWidth: width };
  globalThis.confirm = prompt => { confirms.push(prompt); return true; };
  const deps = {
    h, fill: (node, ...children) => { node.children = children.flat(Infinity).filter(x => x != null); },
    onLeave: fn => leave.push(fn), toast: () => {},
    $app: null, append: () => {}, setHeader: () => {}, go: () => {}, copyBox: () => null, progressBar: () => null,
    isGuest: () => false, isMember: () => false,
    api: async (path, options = {}) => {
      calls.push({path, ...options});
      if (path === '/remote-control') return { enabled: true, projects, discovery: { supported, enabled, limits } };
      if (path.endsWith('/promote')) return { slug: options.body.slug };
      if (path.endsWith('/trust')) return { trust_prompt_open: true };
      if (path.includes('/folders/')) { projects = []; return {removed: true}; }
      if (options.method === 'DELETE') scan = {...scan, status: 'cancelled'};
      return scan;
    },
  };
  const { folderDiscoveryPanel, remoteControlCard } = mountActions(deps);
  const draft = {};
  const roots = settingInput(h, {key: 'remote_control.discovery.roots', type: 'discovery_root_list', writable: true,
    effective: ['C:\\Projects']}, draft);
  assert.equal(roots.tag, 'textarea');
  roots.value = 'C:\\Projects\nD:\\Code'; roots.listeners.change();
  assert.equal(JSON.stringify(draft['remote_control.discovery.roots']), JSON.stringify(['C:\\Projects', 'D:\\Code']));
  const panel = folderDiscoveryPanel({enabled, limits}, () => reloads++);
  assert.ok(button(panel, 'Find folders').disabled);
  assert.match(text(panel), /Discovery is off/);
  assert.match(text(panel), /20,000 directories/);
  enabled = true; panel.updateDiscovery({enabled, limits});
  await button(panel, 'Find folders').onclick();
  assert.ok(button(panel, 'Cancel scan'));
  assert.match(text(panel), /10 directories visited/);
  scan = {...scan, status: 'finished', truncated: true, reason: 'candidate_limit',
    candidates: [{...candidate}], errors: [{location: '*', code: 'directory_unavailable'}]};
  await timers.find(timer => timer.ms === 750).fn(); await settle();
  assert.match(text(panel), /Scan truncated/);
  assert.match(text(panel), /directory_unavailable/);
  assert.ok(button(panel, 'Add folder'));
  assert.equal(button(panel, 'Trust in Claude…'), null);
  const slug = find(panel, node => node.tag === 'input'); slug.value = 'edited-slug';
  await button(panel, 'Add folder').onclick();
  const promotion = calls.find(call => call.path.endsWith('/promote'));
  assert.equal(promotion.body.slug, 'edited-slug');
  assert.equal(promotion.body.confirmed_path, candidate.path);
  assert.equal(JSON.stringify(promotion.body.confirmed_markers), JSON.stringify(candidate.markers));
  assert.match(confirms.at(-1), /C:\\Projects\\Sample/);
  assert.equal(reloads, 1);
  assert.equal(calls.some(call => call.path.endsWith('/trust')), false);
  assert.match(text(panel), /Already added/);
  const disabled = folderDiscoveryPanel({enabled: true, limits}, () => {});
  scan = {...scan, status: 'running', candidates: []};
  await button(disabled, 'Find folders').onclick();
  await button(disabled, 'Cancel scan').onclick();
  assert.equal(calls.at(-1).method, 'DELETE');
  supported = false;
  const unsupported = remoteControlCard(); await settle();
  assert.equal(button(unsupported, 'Find folders'), null);
  supported = true;
  projects = [{ project: 'sample', path: candidate.path, managed: true, running: false, trusted: false, invalid: '' }];
  const rc = remoteControlCard(); await settle();
  assert.ok(button(rc, 'Trust in Claude…'));
  await button(rc, 'Trust in Claude…').onclick(); await settle();
  assert.ok(calls.some(call => call.path.endsWith('/trust')));
  await button(rc, 'Remove folder').onclick(); await settle();
  assert.ok(calls.some(call => call.path === '/remote-control/folders/sample' && call.method === 'DELETE'));
  projects = [{project: 'sample', path: candidate.path, managed: true, invalid: 'root_changed'}];
  const invalid = remoteControlCard(); await settle();
  assert.match(text(invalid), /Invalid folder: root_changed/);
  assert.equal(button(invalid, 'Trust in Claude…'), null);
  for (const close of leave) close();
}
assert.match(css, /\.discovery-path\s*\{[^}]*overflow-wrap: anywhere/);
assert.match(css, /\.discovery-candidate \.row\s*\{[^}]*flex-wrap: wrap/);
console.log('ok: phone and desktop discovery configuration, progress, Add, trust, invalidation and removal');
