// Actions -> Claude Remote Control (#545): the owner starts and stops Remote Control here. Folder trust, forgetting a
// folder and discovery scans are daemon administration, in the Hub and the CLI, so the card offers none of them and
// shows the pointer line instead.
import assert from 'node:assert/strict';
import { mountActions } from '../public/pages/actions.mjs';

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
const findAll = (node, predicate, out = []) => {
  if (!(node instanceof Element)) return out;
  if (predicate(node)) out.push(node);
  for (const child of node.children) findAll(child, predicate, out);
  return out;
};
const buttons = node => findAll(node, n => n.tag === 'button').map(text);
const button = (node, label) => findAll(node, n => n.tag === 'button' && text(n) === label)[0] || null;
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const POINTER = /Daemon administration is in the Hub or the\s+harness\s+CLI\./;

for (const guest of [false, true]) {
  const calls = [], leave = [];
  globalThis.setTimeout = () => 1; globalThis.clearTimeout = () => {};
  globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
  let projects = [];
  const { remoteControlCard, ...rest } = mountActions({
    h, fill: (node, ...children) => { node.children = children.flat(Infinity).filter(x => x != null); },
    onLeave: fn => leave.push(fn), toast: () => {}, $app: null, append: () => {}, setHeader: () => {}, go: () => {},
    progressBar: () => null, isGuest: () => guest,
    api: async (path, options = {}) => {
      calls.push({ path, ...options });
      if (path === '/remote-control') {
        const limits = { visited_directories: 1, candidates: 1, seconds: 1, errors: 1, expiry_seconds: 60 };
        return { enabled: true, projects, discovery: { supported: true, enabled: true, limits } };
      }
      return {};
    },
  });
  assert.equal(rest.folderDiscoveryPanel, undefined, 'no discovery panel is exported');

  projects = [
    { project: 'ready', path: 'C:\\Projects\\Ready', managed: true, running: false, trusted: true, invalid: '' },
    { project: 'live', path: 'C:\\Projects\\Live', managed: true, running: true, trusted: true, invalid: '',
      pairing_url: 'https://claude.ai/code/x', started_at: Date.now() / 1000 },
    { project: 'new', path: 'C:\\Projects\\New', managed: true, running: false, trusted: false, invalid: '' },
    { project: 'gone', path: 'C:\\Projects\\Gone', managed: true, invalid: 'root_changed' },
  ];
  const rc = remoteControlCard(); await settle();
  const shown = text(rc);
  assert.match(shown, /needs one-time Claude workspace trust/);
  for (const label of ['Trust in Claude…', 'Trust window open', 'Remove folder', 'Find folders', 'Add folder', 'Cancel scan']) {
    assert.equal(button(rc, label), null, `no ${label} button`);
  }
  if (guest) {
    assert.deepEqual(buttons(rc), []);
    assert.match(shown, /Demo access cannot start or stop Remote Control\./);
    assert.doesNotMatch(shown, POINTER);
  } else {
    assert.deepEqual(buttons(rc), ['Start', 'Stop'], 'only the trusted folders can start or stop');
    assert.match(shown, /Invalid folder: root_changed/);
    assert.match(shown, POINTER);
    await button(rc, 'Start').onclick(); await settle();
    assert.ok(calls.some(call => call.path === '/remote-control/ready' && call.method === 'POST'));
    await button(rc, 'Stop').onclick(); await settle();
    assert.ok(calls.some(call => call.path === '/remote-control/live/stop' && call.method === 'POST'));
  }
  assert.ok(!calls.some(call => /\/trust$|\/folders\/|\/discovery\//.test(call.path)), 'no trust, forget or discovery calls');
  for (const close of leave) close();
}
console.log('ok: Remote Control starts and stops; trust, forget and discovery are left to the Hub and the CLI');
