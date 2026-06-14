import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  dispatchNativeNotification,
  NATIVE_NOTIFICATION_KINDS,
  sendTestNativeNotification,
  setNativeNotifyEnabled,
  setNativeNotifyKind
} from './native-notifications'
import { $activeSessionId, setActiveSessionId } from './session'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const notify = vi.fn().mockResolvedValue(true)

function setWindowState({ focused = true, hidden = false }: { focused?: boolean; hidden?: boolean }) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden })
  Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => focused })
}

let counter = 0

// Unique session id per call dodges the per-(kind,session) throttle so each
// assertion starts clean.
function freshSession(): string {
  counter += 1

  return `session-${counter}`
}

beforeEach(() => {
  notify.mockClear()
  desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
  setNativeNotifyEnabled(true)

  for (const kind of NATIVE_NOTIFICATION_KINDS) {
    setNativeNotifyKind(kind, true)
  }

  setActiveSessionId(null)
  setWindowState({ focused: false, hidden: true })
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('dispatchNativeNotification focus gating', () => {
  it('fires a completion notification when the window is hidden', () => {
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('fires a completion notification when the window is visible but unfocused (alt-tab)', () => {
    setWindowState({ focused: false, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a completion notification when the window is focused', () => {
    setWindowState({ focused: true, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires an attention notification for an off-screen session even when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'background', title: 'approve' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses an attention notification for the active session when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'on-screen', title: 'approve' })
    expect(notify).not.toHaveBeenCalled()
  })
})

describe('dispatchNativeNotification preferences', () => {
  it('suppresses everything when the master switch is off', () => {
    setNativeNotifyEnabled(false)
    dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses only the disabled kind', () => {
    setNativeNotifyKind('turnDone', false)
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).not.toHaveBeenCalled()

    dispatchNativeNotification({ kind: 'turnError', sessionId: freshSession(), title: 'boom' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('forwards kind and sessionId to the bridge', () => {
    dispatchNativeNotification({ body: 'hi', kind: 'turnError', sessionId: 'abc', title: 'boom' })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ body: 'hi', kind: 'turnError', sessionId: 'abc', title: 'boom' })
    )
  })
})

describe('dispatchNativeNotification throttle', () => {
  it('collapses duplicate kind+session within the throttle window', () => {
    const sessionId = freshSession()
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done again' })
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('sendTestNativeNotification', () => {
  it('fires regardless of focus or active session', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    sendTestNativeNotification('Hermes', 'works')
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('$activeSessionId wiring', () => {
  it('reflects the setter used for gating', () => {
    setActiveSessionId('xyz')
    expect($activeSessionId.get()).toBe('xyz')
  })
})
