// Q5 sketch for #306: the owner's interactive Claude Code on the PC shows the harness's GPU hold state.
// Reads the owner API as the localhost owner (no token), every 30 s; /harness shows the last answer.
import type { Register, EngineInterface } from 'claude-code'

const GPU = 'http://127.0.0.1:8100/api/admin/v1/gpu'

async function poll($: EngineInterface): Promise<string> {
  try {
    const res = await $.http.fetch(GPU)
    if (!res.ok) return `harness: HTTP ${res.status}`
    const gpu = JSON.parse(res.text) as { state?: string; manual?: boolean }
    return `harness: GPU ${gpu.state ?? '?'}${gpu.manual ? ' (manual)' : ''}`
  } catch {
    return 'harness: unreachable'
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    if (!e.isInteractive) return next(e) // nothing to show headless
    await $.command.register({ name: 'harness', description: 'Shows the harness GPU hold state.' })
    $.ui.status(await poll($))
    $.clock.every(30_000, async () => $.ui.status(await poll($)))
    return next(e)
  })

  on('command.run', { command: 'harness' }, async $ => ({ text: await poll($) }))
}
