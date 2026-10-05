import { test, expect } from 'claude-code/testing'

test('/harness reports the GPU state from the owner API', async ($, on) => {
  on('http.fetch', () => ({ value: { status: 200, ok: true, headers: {}, text: '{"state":"paused","manual":true}' } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true } as never)
  const out = await $.command.run({ command: 'harness', args: '' } as never)
  expect(JSON.stringify(out)).toContain('harness: GPU paused (manual)')
})
