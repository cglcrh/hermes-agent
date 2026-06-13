# OpenTUI — Background Activity: agents inspection, background panel, notifications + density

**Status:** SPEC (brainstormed with glitch 2026-06-13) · target branch `feat/opentui-native-engine`
**Hard constraint:** TUI-LAYER ONLY (`ui-opentui/`). **Zero changes to `tui_gateway/server.py` or
`run_agent.py` core.** Build only on gateway events/RPCs that already exist. Everything below was
feasibility-checked against the live gateway surface (see "Gateway surface" §).

## Why

Dogfeedback (screenshots `iznq/qxpe/rpiw/rplj`):
1. **Agents dashboard is too crowded** (`rplj`) — master rows dump each subagent's full multi-line
   prompt; the trace pane is squished. Inspection + transcript reading is "not great."
2. **Background processes are basically invisible** (`qxpe`) — completions leak into the transcript
   as plain lines that read like model output; no panel, no badge, notifications are non-existent.
3. **Input zone is too crowded** (`rpiw`) — status bar + composer + agents tray + completion menu +
   shell note stack under the transcript.

## Design decisions (from the brainstorm)

- **Two SEPARATE surfaces, ONE shared substrate.** Background *agents* (delegated subagents) and
  background *work* (detached runs + OS processes) are visually/feature-wise distinct, but share the
  underlying tracking + notification + badge plumbing.
- **Notifications are multi-channel** on every relevant state change:
  - **(C) inline card** in the transcript — a distinct, colored, collapsed *system card*, clearly
    NOT model output (replaces today's plain-line leak).
  - **(A) ambient badge** — a live count in chrome (status-bar `bg:`/the `⚡ N agents` tray) that
    flashes on change; you pull-to-inspect. Stays visible while things run.
  - **OSC desktop** — reuse the EXISTING `boundary/termChrome.ts` (`notify`, OSC 9/99/777, already
    focus-gated so it only fires when the terminal is blurred).
- **Agents surface = inspection only.** No foregrounding / "become the subagent" (that would change
  core subagent UX — explicitly out of scope). Scannable list + a faithful render of the *already-
  tracked* live activity (goal/model/reasoning/tool calls/progress/summary). No new fetch.
- **Background surface = view + stop.** List runs + OS processes with status/uptime; cancel a run
  (`session.interrupt`/`subagent.interrupt`); **stop-all** OS processes (`process.stop`). Per-process
  kill and per-process logs are NOT exposed as RPCs → out of scope under the no-core rule (noted).
- **Input density is in scope** (own phase).

## Gateway surface we build on (verified — all already exist)

| Need | Mechanism (existing) |
|---|---|
| Background-run lifecycle | `prompt.background` (start), `background.complete` (event) |
| Notifications | `notification.show` / `notification.clear` events — payload `{text, level, kind, ttl_ms, key, id}` |
| Subagent stream | `subagent.spawn_requested/start/thinking/tool/progress/complete` events (store already consumes) |
| List OS processes | `agents.list` RPC → `{processes:[{session_id, command, status, uptime_seconds}]}` |
| Stop OS processes | `process.stop` RPC → `kill_all()` (**all**, not per-process) |
| Cancel a run / subagent | `session.interrupt`, `subagent.interrupt` |
| List active sessions/runs | `session.active_list`, `session.status` |
| Subagent trace (archived) | `spawn_tree.list/load` (already used by `/replay`) |
| OSC desktop notify | `boundary/termChrome.ts` `notify(TermNotification)` |

**Honest limits (no-core constraint):** OS processes get list + stop-all only — no per-process kill
(`process_registry.kill_process` exists but isn't an RPC) and no per-process log tail
(`read_log` isn't an RPC). If the no-core rule is ever relaxed, each is a ~5-line additive `@method`.

## Architecture (Approach 1 — substrate-first)

```
gateway events ──► store: backgroundActivity slice ──► derived counts/state
                          │                                  │
                          ├─► notificationDispatcher ─────────┼─► (C) inline card  (transcript)
                          │     (card + badge + OSC)          ├─► (A) ambient badge (statusBar/tray)
                          │                                   └─► OSC via termChrome.notify
                          ├─► Surface 1: AgentsDashboard (revamp) — list + rich activity pane
                          └─► Surface 2: BackgroundPanel (new)    — runs + processes, stop
```

### Shared substrate (the "underneath" both surfaces use)

- **`logic/backgroundActivity.ts`** (new) — pure model + reducers. Types:
  - `BackgroundRun` (from `prompt.background`/`background.complete`/`session.active_list`):
    `{ id, label, status: 'running'|'complete'|'failed'|'cancelled', startedAt, summary? }`
  - `BackgroundProcess` (from `agents.list`): `{ sessionId, command, status, uptimeSeconds }`
  - `Notification` (from `notification.show`): `{ id, key?, text, level, kind, ttlMs?, at }`
  - Pure helpers: `applyNotification`, `clearNotification(key)`, counts (`runningCount`),
    `mergeProcessList`, dedupe by `key`/`id`. Fully unit-testable (no renderer).
- **`store.ts`** — a `backgroundActivity` slice + event handlers for `notification.show/clear`,
  `background.complete`, and a polled `agents.list` snapshot (poll only while a panel/badge is live,
  or piggyback existing cadence). Existing `subagent.*` handling is untouched.
- **`logic/notificationDispatcher.ts`** (new, pure) — given a state-change, decide the channels:
  returns `{ card?: SystemCard, badge: delta, osc?: TermNotification }`. The boundary calls
  `termChrome.notify` for the OSC part; the store appends the card + bumps the badge.

### Surface 1 — Agents inspection overlay (revamp `view/overlays/agentsDashboard.tsx`)

- **Master list rows = ONE line each:** `<statusGlyph> <truncated goal (truncRight to width)> · <model>`.
  No multi-line prompt dump. Selected row highlighted (existing `▸` + accent).
- **Detail pane = faithful activity transcript** of the selected agent, styled like the main
  transcript (not flat dumped lines): goal+model header, then the trace rendered by *type*
  (reasoning / tool-call+result / progress / final summary), newest last, sticky-bottom, PgUp/PgDn.
  - Requires giving `SubagentInfo.trace` light typing (`{ kind:'tool'|'reasoning'|'progress'|'summary', text }`)
    instead of `string[]`, populated where `subagent.*` events are reduced. Internal data-shape
    change only; no gateway change.
- Keep Esc/q close, ↑↓ select. Reuse theme + `truncRight` from statusBar.

### Surface 2 — Background panel (new `view/overlays/backgroundPanel.tsx`)

- **Two sections:** *Runs* (background agent runs) and *Processes* (OS processes from `agents.list`).
- Each row: status glyph + label/command (truncated) + uptime/elapsed + status.
- **Actions:** `↑↓` select; on a *run* → `c` cancel (`session.interrupt`/`subagent.interrupt`);
  global **stop-all processes** (`x` → `process.stop`, confirm). Esc/q close.
- **Access:** new client slash `/bg` (alias `/background`, `/jobs`) in `logic/slash.ts` CLIENT set →
  `store.openBackgroundPanel()`. Also reachable from the ambient badge.
- Poll `agents.list` on open + on a light interval while open; stop polling on close.

### Notifications (the (C)+(A)+OSC wiring)

- **(C) inline card** — a new transcript element `view/notificationCard.tsx`: a bordered/colored,
  `selectable:false` system card keyed by `notification.id`, level-tinted (`info/warn/error`),
  collapsed to one line by default with the `kind` + `text`; clearable by `notification.clear` key.
  Appended into the message stream as a distinct row type (NOT a plain `system` text line). Replaces
  the current plain-line leak. (`/details` interplay: cards are chrome, always shown, never windowed.)
- **(A) ambient badge** — `statusBar.tsx` `bg: N` segment (already reserved) bound to
  `runningCount()`; the `agentsTray.tsx` count already exists — extend it to "agents + background."
  Flash/recolor on a fresh notification (brief).
- **OSC** — on `notification.show` with a terminal level (complete/failed), call
  `termChrome.notify({title, body})` (already focus-gated). No new escape-sequence code.

### Input-zone density pass (`view/composer.tsx` / `view/App.tsx`)

- Audit what stacks under the transcript and collapse/gate: the `⚡ N agents` tray line folds into
  the ambient badge (shrinks one line); ensure the shell-mode note, completion menu, and status bar
  don't co-stack more than necessary. Concrete rules decided with a tmux density pass (ASCII-mocked,
  approved) — kept minimal; no behavior change, just fewer competing chrome lines.

## Phases (implementation order — each gated + tmux-smoked + committed)

- **P1 — Notification substrate** (`backgroundActivity.ts` + `notificationDispatcher.ts` + store
  slice + `notificationCard.tsx` + badge wiring + OSC call). Highest visible win; the shared core.
- **P2 — Agents inspection revamp** (`agentsDashboard.tsx` + typed `trace`). De-crowds `rplj`.
- **P3 — Background panel** (`backgroundPanel.tsx` + `/bg` + actions). New surface.
- **P4 — Input density pass.** Folds the tray into the badge; trims co-stacked chrome.

## Testing / gates (per phase)

- **Pure logic** (`backgroundActivity`, `notificationDispatcher`, slash `/bg` routing,
  trace-typing) → vitest unit tests, TDD where natural.
- **Views** → headless frame tests (`renderProbe`) for the card, the de-crowded dashboard row
  format, the background panel sections; + **live tmux smoke** (`tmux-pane-screenshot`) for each
  surface using a seeded-store harness (the `uxSmoke` pattern: `store.apply`/`applyInfo`/
  `commitSnapshot` + canned events).
- **Gate** `cd ui-opentui && npm run check` green (judge by real exit, not a piped tail) after each
  phase; rebuild `dist/main.js`; commit `opentui(v6): …` (no attribution) and push per standing instr.

## Out of scope (explicit)

- Foregrounding / "becoming" a subagent (B/C from the brainstorm) — would change core subagent UX.
- Per-process kill + per-process log tail for OS processes — needs additive gateway RPCs (no-core veto).
- "Collect result into transcript" for finished runs — deferred (Q6=B, view+stop only).
- Any change to `tui_gateway/server.py` / `run_agent.py`.
