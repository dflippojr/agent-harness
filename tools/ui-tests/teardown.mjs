import { readdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { unzipSync, zipSync } from 'fflate';
import { stopInstance } from './instance.mjs';
// Trace networks include Authorization and /connect's synthetic bearer token.
// Remove those values before reports leave the disposable test job.
export function sanitizeText(text) {
  return text.replace(/ho-[A-Za-z0-9_-]+/g, '[REDACTED]')
    .replace(/Bearer [A-Za-z0-9_.-]+/g, 'Bearer [REDACTED]');
}
export async function sanitizeTraces(directory) {
  let entries;
  try { entries = await readdir(directory, { withFileTypes: true }); }
  catch (error) { if (error.code === 'ENOENT') return; throw error; }
  for (const entry of entries) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) await sanitizeTraces(filename);
    else if (entry.name.endsWith('.zip')) {
      const files = unzipSync(await readFile(filename));
      for (const name of Object.keys(files)) {
        // Preserve binary screenshots; redact every UTF-8 textual resource and event stream.
        try {
          const text = new TextDecoder('utf-8', { fatal: true }).decode(files[name]);
          files[name] = new TextEncoder().encode(sanitizeText(text));
        } catch (error) { if (!(error instanceof TypeError)) throw error; }
      }
      await writeFile(filename, zipSync(files));
    }
  }
}
export default async function teardown() {
  try { await sanitizeTraces(fileURLToPath(new URL('./test-results/', import.meta.url))); await sanitizeTraces(fileURLToPath(new URL('./playwright-report/', import.meta.url))); }
  finally { stopInstance(); }
}
