import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
export const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
export const python = process.env.PYTHON ?? 'python';
const runName = /^run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// HARNESS_UI_INSTANCE_DIR remains the base shared with CI's later cleanup step. Each invocation gets an
// unreused child directory; the persisted owner lets that step find a run even without its inherited env.
export function createLifecycle({ directory = process.env.HARNESS_UI_INSTANCE_DIR ?? path.join(os.tmpdir(), 'harness-ui-tests'), command = python, commandArgs = [] } = {}) {
  const base = path.resolve(directory);
  function runDirectory(id) {
    if (!runName.test(id)) throw new Error('Invalid UI test run identity');
    return path.join(base, id);
  }
  function record(id) {
    try {
      const owner = JSON.parse(readFileSync(path.join(runDirectory(id), 'owner.json'), 'utf8'));
      if (owner.id !== id || !Number.isSafeInteger(owner.pid) || owner.pid <= 0) throw new Error('Invalid UI test run owner');
      return owner;
    } catch (error) {
      if (error.code === 'ENOENT') return null;
      throw error;
    }
  }
  function stop(id) {
    if (!record(id)) return;
    const root = runDirectory(id);
    const result = spawnSync(command, [...commandArgs, '-m', 'harness.fake_instance', 'stop', '--dir', path.join(root, 'instance')], { cwd: repoRoot, encoding: 'utf8', windowsHide: true });
    if (result.error || result.status !== 0) throw result.error ?? new Error(result.stderr || 'Disposable instance cleanup failed');
    // This UUID directory was allocated by begin(), never reused, and its instance has stopped successfully.
    rmSync(root, { recursive: true, force: true });
  }
  function recoverStale() {
    let entries;
    try { entries = readdirSync(base, { withFileTypes: true }); }
    catch (error) { if (error.code === 'ENOENT') return; throw error; }
    for (const entry of entries) {
      if (!entry.isDirectory() || !runName.test(entry.name)) continue;
      const owner = record(entry.name);
      if (!owner) continue; // Allocation was interrupted before ownership was written: preserve uncertainty.
      try { process.kill(owner.pid, 0); }
      catch (error) {
        // EPERM and every other inspection failure preserve the run; reused live owner pids do too.
        if (error.code === 'ESRCH') stop(entry.name);
      }
    }
  }
  function begin() {
    recoverStale();
    const id = 'run-' + randomUUID();
    mkdirSync(base, { recursive: true });
    mkdirSync(runDirectory(id));
    writeFileSync(path.join(runDirectory(id), 'owner.json'), JSON.stringify({ id, pid: process.pid }), { flag: 'wx' });
    return id;
  }
  function instance(id) {
    if (!record(id)) throw new Error('UI test run ownership is missing');
    return path.join(runDirectory(id), 'instance');
  }
  return { begin, instance, stop, recoverStale };
}
const lifecycle = createLifecycle();
export function beginRun() {
  process.env.HARNESS_UI_RUN_ID = lifecycle.begin();
}
export function instanceDirectory() {
  if (!process.env.HARNESS_UI_RUN_ID) beginRun();
  return lifecycle.instance(process.env.HARNESS_UI_RUN_ID);
}
export function stopInstance() {
  if (process.env.HARNESS_UI_RUN_ID) lifecycle.stop(process.env.HARNESS_UI_RUN_ID);
  else lifecycle.recoverStale();
}
