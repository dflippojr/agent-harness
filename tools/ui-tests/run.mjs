import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import teardown from './teardown.mjs';
import { beginRun } from './instance.mjs';
beginRun();
const cli = fileURLToPath(new URL('./node_modules/@playwright/test/cli.js', import.meta.url));
try {
  process.exitCode = await new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [cli, 'test', ...process.argv.slice(2)], { cwd: fileURLToPath(new URL('.', import.meta.url)), stdio: 'inherit', windowsHide: true });
    child.on('error', reject);
    child.on('exit', code => resolve(code ?? 1));
    for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => child.kill(signal));
  });
} finally { await teardown(); }
