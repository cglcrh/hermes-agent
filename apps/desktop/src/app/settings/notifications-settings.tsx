import { useStore } from '@nanostores/react'

import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { Bell } from '@/lib/icons'
import {
  $nativeNotifyPrefs,
  NATIVE_NOTIFICATION_KINDS,
  sendTestNativeNotification,
  setNativeNotifyEnabled,
  setNativeNotifyKind
} from '@/store/native-notifications'
import { notify } from '@/store/notifications'

import { ListRow, SectionHeading, SettingsContent } from './primitives'

export function NotificationsSettings() {
  const { t } = useI18n()
  const prefs = useStore($nativeNotifyPrefs)
  const copy = t.settings.notifications

  const runTest = async () => {
    triggerHaptic('open')
    const ok = await sendTestNativeNotification(copy.testTitle, copy.testBody)
    notify({ kind: ok ? 'info' : 'error', message: ok ? copy.testSent : copy.testUnsupported })
  }

  return (
    <SettingsContent>
      <SectionHeading icon={Bell} title={copy.title} />
      <p className="mb-2 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {copy.intro}
      </p>

      <ListRow
        action={
          <Switch
            aria-label={copy.enableAll}
            checked={prefs.enabled}
            onCheckedChange={value => {
              triggerHaptic('selection')
              setNativeNotifyEnabled(value)
            }}
          />
        }
        description={copy.enableAllDesc}
        title={copy.enableAll}
      />

      <div className="my-1 h-px bg-border/30" />

      {NATIVE_NOTIFICATION_KINDS.map(kind => {
        const kindCopy = copy.kinds[kind]

        return (
          <ListRow
            action={
              <Switch
                aria-label={kindCopy.label}
                checked={prefs.enabled && prefs.kinds[kind]}
                disabled={!prefs.enabled}
                onCheckedChange={value => {
                  triggerHaptic('selection')
                  setNativeNotifyKind(kind, value)
                }}
              />
            }
            description={kindCopy.description}
            key={kind}
            title={kindCopy.label}
          />
        )
      })}

      <div className="mt-4 flex flex-col gap-2">
        <Button
          className="self-start"
          onClick={() => void runTest()}
          size="sm"
          type="button"
          variant="outline"
        >
          <Bell />
          {copy.test}
        </Button>
        <p className="text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
          {copy.focusedHint}
        </p>
      </div>
    </SettingsContent>
  )
}
