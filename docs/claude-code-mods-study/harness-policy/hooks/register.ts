// Study prototype for #306, not a product. Reports what a headless worker does to the per-session relay
// stub (127.0.0.1:8790) and screens a few commands as a second layer behind the harness's own approvals.
import type { Register, EngineInterface } from 'claude-code'

const RELAY = 'http://127.0.0.1:8790'
const MOD_DIR = '/opt/harness-mods/'
// Second-layer screen: network tools and writes to the mod's own folder. The harness stays authoritative.
const EGRESS = /\b(curl|wget|nc|ncat|ssh|scp)\b/
const TOUCHES_MOD = /\/opt\/harness-mods\b/

async function report($: EngineInterface, event: string, data: unknown): Promise<void> {
  try {
    await $.http.fetch(`${RELAY}/events`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ event, at: Date.now(), data }),
    })
  } catch (err) {
    // The relay unreachable must never stop the session.
  }
}

function brief(input: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const key of ['command', 'file_path', 'old_string', 'new_string']) {
    if (typeof input[key] === 'string') out[key] = String(input[key]).slice(0, 120)
  }
  return out
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const t0 = Date.now()
    let status: unknown
    try {
      const res = await $.http.fetch(`${RELAY}/status`)
      status = { status: res.status, body: JSON.parse(res.text), ms: Date.now() - t0 }
    } catch (err) {
      status = { error: String(err), ms: Date.now() - t0 }
    }
    // UI surfaces in a headless session: these should no-op, not fail the hook.
    let ui = 'ok'
    try {
      $.ui.status('harness: study mod loaded')
      await $.ui.toast('harness: study mod loaded')
    } catch (err) {
      ui = String(err)
    }
    const version = await $.session.version()
    await report($, 'session.start', { cwd: e.cwd, surface: e.surface, isInteractive: e.isInteractive, version, status, ui })

    if ((await $.env.get('STUDY_PROBE')) === '1') {
      // Q3: a slow relay answer. $ calls do not count against the 10 s hook budget.
      const t1 = Date.now()
      try {
        const slow = await $.http.fetch(`${RELAY}/slow?ms=12000`)
        await report($, 'probe.slow-fetch', { status: slow.status, ms: Date.now() - t1 })
      } catch (err) {
        await report($, 'probe.slow-fetch', { error: String(err), ms: Date.now() - t1 })
      }
      // Q4: a mod runs host commands with no permission prompt.
      const ran = await $.process.run(['sh', '-c', 'id -u; echo from-mod > /workspace/mod-ran.txt'])
      await report($, 'probe.process-run', { exitCode: ran.exitCode, stdout: ran.stdout.trim() })
      // Egress from the mod's own fetch: the session has no route out.
      try {
        const out = await $.http.fetch('https://example.com/')
        await report($, 'probe.egress', { status: out.status })
      } catch (err) {
        await report($, 'probe.egress', { error: String(err).slice(0, 200) })
      }
    }
    return next(e)
  })

  on('turn.start', async ($, e, next) => {
    await report($, 'turn.start', { turnId: e.turnId, text: e.text.slice(0, 80) })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    await report($, 'turn.complete', { reason: e.reason })
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await report($, 'session.end', { reason: e.reason })
    return next(e)
  })

  on('tool.check', async ($, e, next) => {
    const verdict = await next(e)
    await report($, 'tool.check', { tool: e.tool, decision: (verdict as { decision?: string }).decision })
    return verdict
  })

  on('tool.call', async ($, e, next) => {
    const input = brief(e as unknown as Record<string, unknown>)
    const text = JSON.stringify(input)
    if (TOUCHES_MOD.test(text) || (e.tool === 'Bash' && EGRESS.test(text))) {
      const deny = `harness-policy: ${e.tool} refused by the second layer`
      await report($, 'tool.call.denied', { tool: e.tool, input })
      return { deny }
    }
    await report($, 'tool.call', { tool: e.tool, tool_use_id: e.tool_use_id, input })
    const result = await next(e)
    await report($, 'tool.result', {
      tool: e.tool,
      deny: result.deny,
      isError: result.deny === undefined ? result.isError ?? false : undefined,
      text: result.deny === undefined ? String(result.text ?? '').slice(0, 160) : undefined,
    })
    return result
  })
}
