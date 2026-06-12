#!/usr/bin/env node
// memwatch-report — aggregate the per-session NDJSON written by the TUI's
// in-process sampler (ui-opentui/src/boundary/memlog.ts) into one fleet table.
//
// Usage: node memwatch-report.mjs [dir]    (default ~/.hermes/logs/memwatch)
// Output: one row per session file — start, duration, baseline/peak/last RSS,
// peak mounted rows, and a crude steady-state slope (MB/h over the last half) —
// plus anomaly flags: SLOPE (last-half slope > 20MB/h), PEAK (> 450MB),
// MOUNTED (peak mounted rows > 200 — windowing should bound ~30-120).
import { readdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const dir = process.argv[2] ?? join(homedir(), '.hermes', 'logs', 'memwatch')

let files = []
try {
  files = readdirSync(dir).filter(f => f.endsWith('.jsonl')).sort()
} catch {
  console.error(`no memwatch dir at ${dir} — enable with HERMES_TUI_DIAGNOSTICS=1 (or HERMES_TUI_MEMLOG=1)`)
  process.exit(1)
}
if (!files.length) {
  console.error(`no sessions logged yet in ${dir}`)
  process.exit(1)
}

const rows = []
for (const f of files) {
  const samples = []
  for (const line of readFileSync(join(dir, f), 'utf8').split('\n')) {
    if (!line.trim()) continue
    try { samples.push(JSON.parse(line)) } catch { /* torn write */ }
  }
  if (samples.length < 2) continue
  const rss = samples.map(s => s.rss_kb / 1024)
  const peak = Math.max(...rss)
  const durMin = (samples.at(-1).t - samples[0].t) / 60
  // steady-state slope: least-squares over the last half of the samples
  const half = samples.slice(Math.floor(samples.length / 2))
  const t0 = half[0].t
  const xs = half.map(s => (s.t - t0) / 3600)
  const ys = half.map(s => s.rss_kb / 1024)
  const n = xs.length
  const mx = xs.reduce((a, b) => a + b, 0) / n
  const my = ys.reduce((a, b) => a + b, 0) / n
  const denom = xs.reduce((a, x) => a + (x - mx) ** 2, 0)
  const slope = denom > 0 ? xs.reduce((a, x, i) => a + (x - mx) * (ys[i] - my), 0) / denom : 0
  const peakMounted = Math.max(...samples.map(s => s.peak_mounted ?? 0))
  const flags = []
  if (slope > 20 && durMin > 10) flags.push('SLOPE')
  if (peak > 450) flags.push('PEAK')
  if (peakMounted > 200) flags.push('MOUNTED')
  rows.push({
    session: f.replace('.jsonl', ''),
    start: new Date(samples[0].t * 1000).toISOString().slice(0, 16),
    min: Math.round(durMin),
    base: Math.round(rss[0]),
    peak: Math.round(peak),
    last: Math.round(rss.at(-1)),
    mounted: peakMounted,
    'MB/h': Math.round(slope * 10) / 10,
    flags: flags.join(',') || '—'
  })
}

console.table(rows)
const flagged = rows.filter(r => r.flags !== '—')
console.log(flagged.length
  ? `\n${flagged.length} session(s) flagged — investigate with bench/live-attach.sh <pid> --heap on a live one.`
  : `\nall ${rows.length} sessions healthy (no slope/peak/mounted anomalies).`)
