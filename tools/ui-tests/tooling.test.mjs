import { test, mock } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { zipSync, unzipSync } from 'fflate';
import { sanitizeText, sanitizeTraces } from './teardown.mjs';
import { createLifecycle, repoRoot } from './instance.mjs';
import { spawnSync } from 'node:child_process';

async function lifecycleTest(body) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'harness-ui-lifecycle-'));
  try {
    const log = path.join(directory, 'stops.jsonl');
    const stub = path.join(directory, 'python-stub.mjs');
    await writeFile(stub, `
      import { appendFileSync, existsSync, readFileSync, rmSync } from 'node:fs';
      const args = process.argv.slice(2);
      if (args.slice(0, 3).join(' ') !== '-m harness.fake_instance stop') process.exit(2);
      const root = args[args.indexOf('--dir') + 1];
      const handle = root + '/instance.json';
      const identity = existsSync(handle) ? JSON.parse(readFileSync(handle, 'utf8')) : null;
      appendFileSync(${JSON.stringify(log)}, JSON.stringify({ root, identity }) + '\\n');
      if (identity?.fail) { console.error('cannot verify process'); process.exit(1); }
      rmSync(root, { recursive: true, force: true });
    `);
    const caller = (base = path.join(directory, 'base')) => createLifecycle({ directory: base, command: process.execPath, commandArgs: [stub] });
    const lifecycle = caller();
    const stops = async () => {
      try { return (await readFile(log, 'utf8')).trim().split('\n').map(line => JSON.parse(line)); }
      catch (error) { if (error.code === 'ENOENT') return []; throw error; }
    };
    const seed = async (id, extra = {}) => {
      const root = lifecycle.instance(id);
      await mkdir(root);
      await writeFile(path.join(root, 'instance.json'), JSON.stringify({ id, pid: 123, pid_created: 42, ...extra }));
      return root;
    };
    await body({ lifecycle, caller, stops, seed });
  } finally {
    // mkdtemp allocated this scratch directory; no server or external data is involved.
    await rm(directory, { recursive: true, force: true });
  }
}

test('overlapping runs never stop each other, including repeated global/finally teardown', async () => {
  await lifecycleTest(async ({ lifecycle, caller, stops, seed }) => {
    const first = lifecycle.begin();
    const firstRoot = await seed(first);
    const other = caller();
    const second = other.begin();
    await seed(second);
    assert.notEqual(firstRoot, lifecycle.instance(second));
    assert.deepEqual(await stops(), []); // Starting B does not pre-clean A.
    lifecycle.recoverStale(); // CI cleanup without an inherited run identity skips both live owners.
    assert.deepEqual(await stops(), []);
    other.stop(second);
    other.stop(second);
    assert.deepEqual((await stops()).map(stop => stop.identity.id), [second]);
    assert.equal(JSON.parse(await readFile(path.join(firstRoot, 'instance.json'), 'utf8')).id, first);
    const replacement = other.begin();
    const replacementRoot = await seed(replacement);
    lifecycle.stop(first); // A's late teardown cannot stop B's replacement either.
    lifecycle.stop(first);
    assert.deepEqual((await stops()).map(stop => stop.identity.id), [second, first]);
    assert.equal(JSON.parse(await readFile(path.join(replacementRoot, 'instance.json'), 'utf8')).id, replacement);
    other.stop(replacement);
  });
});

test('failure before server startup cleans only its own allocation', async () => {
  await lifecycleTest(async ({ lifecycle, stops, seed }) => {
    const first = lifecycle.begin();
    const firstRoot = await seed(first);
    const failed = lifecycle.begin();
    const failedRoot = lifecycle.instance(failed);
    lifecycle.stop(failed); // Busy ports or a spawn failure left no instance handle.
    lifecycle.stop(failed);
    assert.deepEqual(await stops(), [{ root: failedRoot, identity: null }]);
    assert.equal(JSON.parse(await readFile(path.join(firstRoot, 'instance.json'), 'utf8')).id, first);
    lifecycle.stop(first);
  });
});

test('stale owner recovery uses the recorded run identity and preserves live owners', async () => {
  await lifecycleTest(async ({ lifecycle, stops, seed }) => {
    const stale = lifecycle.begin();
    const staleRoot = await seed(stale);
    const exited = spawnSync(process.execPath, ['-e', ''], { windowsHide: true });
    assert.equal(exited.status, 0);
    await writeFile(path.join(path.dirname(staleRoot), 'owner.json'), JSON.stringify({ id: stale, pid: exited.pid }));
    const live = lifecycle.begin(); // Recovers stale leftovers before allocating the new run.
    const liveRoot = await seed(live);
    assert.deepEqual((await stops()).map(stop => stop.identity.id), [stale]);
    lifecycle.recoverStale(); // The separate CI cleanup path is safe to repeat.
    assert.deepEqual((await stops()).map(stop => stop.identity.id), [stale]);
    assert.equal(JSON.parse(await readFile(path.join(liveRoot, 'instance.json'), 'utf8')).id, live);
    lifecycle.stop(live);
  });
});

test('failed shutdown retains the run ownership and handle for a later cleanup', async () => {
  await lifecycleTest(async ({ lifecycle, stops, seed }) => {
    const id = lifecycle.begin();
    const root = await seed(id, { fail: true });
    assert.throws(() => lifecycle.stop(id), /cannot verify process/);
    assert.equal(lifecycle.instance(id), root);
    assert.equal(JSON.parse(await readFile(path.join(root, 'instance.json'), 'utf8')).id, id);
    assert.equal((await stops()).length, 1);
  });
});

test('unreadable owner metadata never authorizes stale recovery', async () => {
  await lifecycleTest(async ({ lifecycle, stops, seed }) => {
    const id = lifecycle.begin();
    const root = await seed(id);
    const inspection = mock.method(process, 'kill', () => { throw Object.assign(new Error('access denied'), { code: 'EPERM' }); });
    try { lifecycle.recoverStale(); }
    finally { inspection.mock.restore(); }
    assert.deepEqual(await stops(), []);
    assert.equal(JSON.parse(await readFile(path.join(root, 'instance.json'), 'utf8')).id, id);
    lifecycle.stop(id);
  });
});

test('interrupted ownership publication preserves the allocation without blocking other runs', async () => {
  await lifecycleTest(async ({ lifecycle, stops, seed }) => {
    const incomplete = lifecycle.begin();
    const root = await seed(incomplete);
    const ownerFile = path.join(path.dirname(root), 'owner.json');
    await writeFile(ownerFile, '{"id":'); // A partially written record from an older runner.
    const next = lifecycle.begin();
    assert.deepEqual(await stops(), []);
    assert.equal(JSON.parse(await readFile(path.join(root, 'instance.json'), 'utf8')).id, incomplete);
    await rm(ownerFile);
    await writeFile(path.join(path.dirname(root), 'owner.tmp'), '{"id":'); // Interrupted before atomic publication.
    lifecycle.recoverStale();
    assert.deepEqual(await stops(), []);
    lifecycle.stop(next);
    assert.equal(JSON.parse(await readFile(path.join(root, 'instance.json'), 'utf8')).id, incomplete);
  });
});

test('relative base paths resolve identically from npm and repository-root cleanup', async () => {
  await lifecycleTest(async ({ lifecycle, caller, stops, seed }) => {
    const id = lifecycle.begin();
    const root = await seed(id);
    const relative = path.relative(repoRoot, path.dirname(path.dirname(root)));
    const previous = process.cwd();
    try {
      process.chdir(path.join(repoRoot, 'tools', 'ui-tests'));
      const npmCaller = caller(relative);
      assert.equal(npmCaller.instance(id), root);
      process.chdir(repoRoot);
      const cleanupCaller = caller(relative);
      assert.equal(cleanupCaller.instance(id), root);
      cleanupCaller.stop(id);
      assert.deepEqual((await stops()).map(stop => stop.identity.id), [id]);
    } finally { process.chdir(previous); }
  });
});

test('trace redaction removes owner tokens from network events and connect resources', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'harness-trace-redaction-'));
  try {
    await mkdir(path.join(directory, 'nested'));
    const token = 'ho-throwaway_A1-b2';
    const binary = new Uint8Array([137, 80, 78, 71, 255]);
    const trace = zipSync({
      'trace.network': new TextEncoder().encode(JSON.stringify({ headers: [{ name: 'Authorization', value: 'Bearer ' + token }] })),
      'resources/connect': new TextEncoder().encode('<script>localStorage.setItem("harness.ownerToken","' + token + '");</script>'),
      'resources/image.png': binary,
    });
    const filename = path.join(directory, 'nested', 'trace.zip');
    await writeFile(filename, trace);
    await sanitizeTraces(directory);
    const clean = unzipSync(await readFile(filename));
    for (const name of ['trace.network', 'resources/connect']) {
      const text = new TextDecoder().decode(clean[name]);
      assert.ok(!text.includes(token));
      assert.ok(text.includes('[REDACTED]'));
    }
    assert.deepEqual(clean['resources/image.png'], binary);
    await sanitizeTraces(path.join(directory, 'absent'));
    assert.equal(sanitizeText('Bearer opaque.value-1'), 'Bearer [REDACTED]');
  } finally {
    // mkdtemp is the sole source of this verified owned scratch path.
    await rm(directory, { recursive: true, force: true });
  }
});
