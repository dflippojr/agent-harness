import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
export const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
export const instanceDir = process.env.HARNESS_UI_INSTANCE_DIR ?? path.join(os.tmpdir(), 'harness-ui-tests');
export const python = process.env.PYTHON ?? 'python';
export function stopInstance() {
  const result = spawnSync(python, ['-m', 'harness.fake_instance', 'stop', '--dir', instanceDir], { cwd: repoRoot, encoding: 'utf8', windowsHide: true });
  if (result.error || result.status !== 0) throw result.error ?? new Error(result.stderr || 'Disposable instance cleanup failed');
}
