import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { zipSync, unzipSync } from 'fflate';
import { sanitizeText, sanitizeTraces } from './teardown.mjs';

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
