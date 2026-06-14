import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

import { $activeSessionId } from './session'

// Native OS notifications (Electron `Notification`) — distinct from the in-app
// toast feed in `notifications.ts`. Each kind is independently toggleable and
// gated on window focus so we never interrupt the user about something already
// on screen.
export type NativeNotificationKind = 'approval' | 'backgroundDone' | 'input' | 'turnDone' | 'turnError'

export const NATIVE_NOTIFICATION_KINDS: readonly NativeNotificationKind[] = [
  'approval',
  'input',
  'turnDone',
  'turnError',
  'backgroundDone'
]

// Attention kinds are blocking prompts: they surface even while the app is
// focused, as long as they belong to a session other than the one on screen.
// Completion kinds only fire when the window is hidden.
const ATTENTION_KINDS = new Set<NativeNotificationKind>(['approval', 'input'])

export interface NativeNotificationPrefs {
  enabled: boolean
  kinds: Record<NativeNotificationKind, boolean>
}

const STORAGE_KEY = 'hermes:native-notifications'

const DEFAULT_PREFS: NativeNotificationPrefs = {
  enabled: true,
  kinds: { approval: true, backgroundDone: true, input: true, turnDone: true, turnError: true }
}

function readPrefs(): NativeNotificationPrefs {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return DEFAULT_PREFS
  }

  try {
    const parsed = JSON.parse(raw) as Partial<NativeNotificationPrefs>
    const kinds = { ...DEFAULT_PREFS.kinds }

    if (parsed.kinds && typeof parsed.kinds === 'object') {
      for (const kind of NATIVE_NOTIFICATION_KINDS) {
        const value = parsed.kinds[kind]

        if (typeof value === 'boolean') {
          kinds[kind] = value
        }
      }
    }

    return {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : DEFAULT_PREFS.enabled,
      kinds
    }
  } catch {
    return DEFAULT_PREFS
  }
}

export const $nativeNotifyPrefs = atom<NativeNotificationPrefs>(readPrefs())

function writePrefs(next: NativeNotificationPrefs) {
  $nativeNotifyPrefs.set(next)
  persistString(STORAGE_KEY, JSON.stringify(next))
}

export function setNativeNotifyEnabled(enabled: boolean) {
  writePrefs({ ...$nativeNotifyPrefs.get(), enabled })
}

export function setNativeNotifyKind(kind: NativeNotificationKind, on: boolean) {
  const prev = $nativeNotifyPrefs.get()
  writePrefs({ ...prev, kinds: { ...prev.kinds, [kind]: on } })
}

// Light throttle so replayed events can't stack duplicate toasts for the same
// session+kind within a tight window.
const THROTTLE_MS = 1000
const lastFiredAt = new Map<string, number>()

function windowHidden(): boolean {
  return typeof document !== 'undefined' && document.hidden
}

function shouldFire(kind: NativeNotificationKind, sessionId?: null | string): boolean {
  if (windowHidden()) {
    return true
  }

  // Window is visible: only an attention kind for an off-screen session breaks
  // through. Everything else is already in front of the user.
  if (!ATTENTION_KINDS.has(kind)) {
    return false
  }

  return Boolean(sessionId) && sessionId !== $activeSessionId.get()
}

export interface NativeNotificationInput {
  kind: NativeNotificationKind
  title: string
  body?: string
  sessionId?: null | string
  silent?: boolean
}

export function dispatchNativeNotification(input: NativeNotificationInput): void {
  const prefs = $nativeNotifyPrefs.get()

  if (!prefs.enabled || !prefs.kinds[input.kind]) {
    return
  }

  if (!shouldFire(input.kind, input.sessionId)) {
    return
  }

  const throttleKey = `${input.kind}:${input.sessionId ?? ''}`
  const now = Date.now()
  const prev = lastFiredAt.get(throttleKey)

  if (prev !== undefined && now - prev < THROTTLE_MS) {
    return
  }

  lastFiredAt.set(throttleKey, now)

  void window.hermesDesktop?.notify({
    body: input.body,
    kind: input.kind,
    sessionId: input.sessionId ?? undefined,
    silent: input.silent,
    title: input.title
  })
}

// Settings "send test" button — bypasses gating so the user always sees the
// result of flipping a toggle, even with the window focused. Returns whether
// the OS accepted the notification (false = unsupported / no desktop bridge) so
// the panel can surface feedback instead of failing silently.
export async function sendTestNativeNotification(title: string, body: string): Promise<boolean> {
  const bridge = window.hermesDesktop

  if (!bridge?.notify) {
    return false
  }

  try {
    return await bridge.notify({ body, kind: 'turnDone', title })
  } catch {
    return false
  }
}
