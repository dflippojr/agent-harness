import { spawn } from 'node:child_process';
import { instanceDir, python, repoRoot } from './instance.mjs';
const child = spawn(python, ['-m', 'harness.fake_instance', '--dir', instanceDir, '--port', '18800', '--web-port', '18801'], { cwd: repoRoot, stdio: ['ignore', 'ignore', 'inherit'], windowsHide: true });
child.on('error', error => { console.error(error.message); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
