/**
 * Slash command system — the SOLID side (spec §1; mirrors Ink
 * `app/createSlashHandler.ts` + `domain/slash.ts`). Plain functions/data, NOT
 * Effect; the boundary injects a Promise-returning `request` so dispatch can call
 * `slash.exec` / `command.dispatch` / `commands.catalog`.
 *
 * Dispatch ladder (Ink parity):
 *   1. client-local command (the TUI-only set — handled in-process)
 *   2. `slash.exec {command, session_id}` → `{output, warning?}` → system line
 *   3. on reject → `command.dispatch {arg, name, session_id}` → typed action
 *      (exec/plugin → system · alias → re-dispatch · skill/send → submit a turn ·
 *       prefill → notice). Pager routing for long output lands with Phase 5a; for
 *      now long output is shown as a (multi-line) system message.
 */
export interface ParsedSlash {
  name: string
  arg: string
}

/** Parse `/name rest…` → {name, arg}; null if not a slash command. */
export function parseSlash(input: string): ParsedSlash | null {
  if (!input.startsWith('/')) return null
  const body = input.slice(1).trimStart()
  if (!body) return null
  const sp = body.indexOf(' ')
  return sp === -1 ? { arg: '', name: body } : { arg: body.slice(sp + 1).trim(), name: body.slice(0, sp) }
}

/** The host capabilities the dispatcher needs (wired by the entry boundary). */
export interface SlashContext {
  /** Server RPC (resolves with the result, rejects on GatewayError). */
  readonly request: (method: string, params: Record<string, unknown>) => Promise<unknown>
  readonly sessionId: () => string | undefined
  readonly pushSystem: (text: string) => void
  /** Submit a user turn (skill/send dispatch results). */
  readonly submit: (text: string) => void
  /** Open a local Y/N confirm; `onConfirm` runs on Yes. */
  readonly confirm: (message: string, onConfirm: () => void) => void
  readonly clearTranscript: () => void
  readonly quit: () => void
  /** Recent log lines for `/logs` (the ring buffer). */
  readonly logTail: () => string[]
}

function readStr(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== 'object') return undefined
  const v = (value as { [k: string]: unknown })[key]
  return typeof v === 'string' ? v : undefined
}

const CLIENT_HELP = [
  '/help — list commands',
  '/clear, /new — clear the transcript (confirm)',
  '/logs — recent engine log lines',
  '/quit, /exit — quit',
  '(other /commands run on the gateway)'
].join('\n')

type ClientHandler = (arg: string, ctx: SlashContext) => void | Promise<void>

/** The TUI-only client commands (run in-process, never hit the gateway). */
const CLIENT: Record<string, ClientHandler> = {
  clear: (_arg, ctx) => ctx.confirm('Clear the transcript?', ctx.clearTranscript),
  exit: (_arg, ctx) => ctx.quit(),
  help: async (_arg, ctx) => {
    // Prefer the live catalog; fall back to the client list if it's unavailable.
    try {
      const cat = await ctx.request('commands.catalog', {})
      ctx.pushSystem(renderCatalog(cat) || CLIENT_HELP)
    } catch {
      ctx.pushSystem(CLIENT_HELP)
    }
  },
  logs: (_arg, ctx) => ctx.pushSystem(ctx.logTail().slice(-40).join('\n') || '(log empty)'),
  new: (_arg, ctx) => ctx.confirm('Start fresh? (clears the transcript)', ctx.clearTranscript),
  quit: (_arg, ctx) => ctx.quit()
}

/** Render the gateway `commands.catalog` into a help block (loose-typed read).
 *  The TUI catalog shape is `{ pairs: [["/name","desc"], …], canon, categories }`
 *  (tui_gateway/server.py `commands.catalog`). */
function renderCatalog(cat: unknown): string {
  if (!cat || typeof cat !== 'object') return ''
  const pairs = (cat as { pairs?: unknown }).pairs
  if (!Array.isArray(pairs)) return ''
  const lines = pairs
    .map(pair => {
      if (!Array.isArray(pair) || typeof pair[0] !== 'string') return null
      const desc = typeof pair[1] === 'string' ? pair[1] : ''
      return desc ? `${pair[0]} — ${desc}` : pair[0]
    })
    .filter((l): l is string => l !== null)
  return lines.length ? lines.join('\n') : ''
}

function handleDispatchResult(parsed: ParsedSlash, raw: unknown, ctx: SlashContext): void {
  const type = readStr(raw, 'type')
  const argTail = parsed.arg ? ` ${parsed.arg}` : ''
  switch (type) {
    case 'exec':
    case 'plugin':
      ctx.pushSystem(readStr(raw, 'output') || '(no output)')
      return
    case 'alias': {
      const target = readStr(raw, 'target')
      if (target) void dispatchSlash(`/${target}${argTail}`, ctx)
      return
    }
    case 'skill':
    case 'send': {
      const notice = readStr(raw, 'notice')
      if (notice) ctx.pushSystem(notice)
      const message = readStr(raw, 'message')
      if (message?.trim()) ctx.submit(message)
      else ctx.pushSystem(`/${parsed.name}: empty message`)
      return
    }
    case 'prefill': {
      // /undo etc. — composer prefill lands with the composer-ref plumbing; show it for now.
      const message = readStr(raw, 'message')
      ctx.pushSystem(message ? `(edit & resubmit) ${message}` : `/${parsed.name}: nothing to prefill`)
      return
    }
    default:
      ctx.pushSystem(`error: invalid response: command.dispatch`)
  }
}

/** Dispatch a `/command` through the ladder. Returns once the (async) work settles. */
export async function dispatchSlash(input: string, ctx: SlashContext): Promise<void> {
  const parsed = parseSlash(input)
  if (!parsed) return

  const client = CLIENT[parsed.name]
  if (client) {
    await client(parsed.arg, ctx)
    return
  }

  const sid = ctx.sessionId()
  try {
    const result = await ctx.request('slash.exec', { command: input.slice(1), session_id: sid })
    const output = readStr(result, 'output') || `/${parsed.name}: no output`
    const warning = readStr(result, 'warning')
    ctx.pushSystem(warning ? `warning: ${warning}\n${output}` : output)
  } catch {
    try {
      const raw = await ctx.request('command.dispatch', { arg: parsed.arg, name: parsed.name, session_id: sid })
      handleDispatchResult(parsed, raw, ctx)
    } catch (error) {
      ctx.pushSystem(`error: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
}
