/**
 * ConfirmPrompt — a LOCAL (non-gateway) Y/N dialog (spec §2a). Driven by a local
 * callback, not an RPC: y/Enter → confirm, n/Esc/Ctrl+C → cancel. Used by client
 * slash commands like /clear and /new.
 */
import { useKeyboard } from '@opentui/solid'

import { useTheme } from '../theme.tsx'

export function ConfirmPrompt(props: { message: string; onYes: () => void; onNo: () => void }) {
  const theme = useTheme()
  useKeyboard(key => {
    if (key.name === 'y' || key.name === 'return') props.onYes()
    else if (key.name === 'n' || key.name === 'escape' || (key.ctrl && key.name === 'c')) props.onNo()
  })

  return (
    <box
      style={{ borderColor: theme().color.border, flexDirection: 'column', flexShrink: 0, marginTop: 1, padding: 1 }}
      border
    >
      <text fg={theme().color.warn}>
        <b>{props.message}</b>
      </text>
      <text fg={theme().color.muted}>y/Enter confirm · n/Esc cancel</text>
    </box>
  )
}
