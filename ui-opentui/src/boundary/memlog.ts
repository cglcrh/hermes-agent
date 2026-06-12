/**
 * memlog — in-process 1Hz memory self-sampling to NDJSON.
 *
 * The fleet-monitoring answer to "attach live-attach.sh to all 5–10 of my
 * sessions": instead of an external watcher chasing pids, every TUI session
 * logs its OWN samples when enabled, keyed by pid + boot time, into
 * `~/.hermes/logs/memwatch/`. Aggregate across sessions with
 * `bench/memwatch-report.mjs`.
 *
 * Gating (docs/opentui-env-flags.md): `HERMES_TUI_MEMLOG` — defaults to the
 * `HERMES_TUI_DIAGNOSTICS` master switch, individually overridable either way.
 * One `export HERMES_TUI_DIAGNOSTICS=1` in a dev's shell rc therefore covers
 * every session they ever start; regular users write nothing.
 *
 * Cost when on: one `process.memoryUsage()` + one short append per second
 * (~60 bytes/s, ~5MB/day across ten busy sessions). The interval is unref'd —
 * it never keeps the process alive. Every failure path disables the logger
 * silently (diagnostics must never break the TUI). Retention: files older
 * than 14 days are pruned at start, best-effort.
 *
 * Sample shape (one JSON object per line):
 *   { t, rss_kb, heap_used_kb, external_kb, mounted, peak_mounted }
 * `mounted`/`peak_mounted` come from the windowing DEV counters
 * (logic/window.ts) — they update whenever windowing is active, independent
 * of the WINDOW_STATS exposure flag.
 */
import { appendFileSync, mkdirSync, readdirSync, statSync, unlinkSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

import { diagnosticsEnabled, envFlag } from '../logic/env.ts'
import { windowRowStats } from '../logic/window.ts'

const RETENTION_DAYS = 14
const SAMPLE_MS = 1000

function memwatchDir(): string {
  const home = process.env.HERMES_HOME?.trim()
  const base = home && home.length > 0 ? home : join(homedir(), '.hermes')
  return join(base, 'logs', 'memwatch')
}

function pruneOld(dir: string): void {
  const cutoff = Date.now() - RETENTION_DAYS * 24 * 3600 * 1000
  try {
    for (const name of readdirSync(dir)) {
      if (!name.endsWith('.jsonl')) continue
      const p = join(dir, name)
      try {
        if (statSync(p).mtimeMs < cutoff) unlinkSync(p)
      } catch {
        /* best-effort */
      }
    }
  } catch {
    /* best-effort */
  }
}

/** Start the self-sampler (no-op unless enabled). Returns a stop function. */
export function startMemlog(): () => void {
  if (!envFlag(process.env.HERMES_TUI_MEMLOG, diagnosticsEnabled())) return () => {}
  try {
    const dir = memwatchDir()
    mkdirSync(dir, { recursive: true })
    pruneOld(dir)
    const boot = new Date().toISOString().replace(/[:.]/g, '').slice(0, 15)
    const file = join(dir, `${boot}-${process.pid}.jsonl`)
    const timer = setInterval(() => {
      try {
        const m = process.memoryUsage()
        const w = windowRowStats()
        const line = JSON.stringify({
          t: Math.floor(Date.now() / 1000),
          rss_kb: Math.floor(m.rss / 1024),
          heap_used_kb: Math.floor(m.heapUsed / 1024),
          external_kb: Math.floor(m.external / 1024),
          mounted: w.mounted,
          peak_mounted: w.peakMounted
        })
        appendFileSync(file, line + '\n')
      } catch {
        clearInterval(timer) // a failing diagnostic must not retry forever
      }
    }, SAMPLE_MS)
    timer.unref?.()
    return () => clearInterval(timer)
  } catch {
    return () => {}
  }
}
