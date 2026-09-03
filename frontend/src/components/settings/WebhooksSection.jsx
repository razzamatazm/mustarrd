import { Stack, Text, TextInput } from '@mantine/core'

import { LabelWithTooltip, SectionHeader, SubGroup } from './SettingsPrimitives'

const MAX_WEBHOOK_URL_LENGTH = 1000

// One field per event the backend publishes, in the order it lists them.
export const WEBHOOK_FIELDS = [
  {
    field: 'webhook_url_recording_started',
    label: 'Recording started',
    description: 'Sent the moment Mustarrd begins pulling a recording.',
    tooltip: 'Useful for turning on a light, posting to a chat room, or logging that a recording is underway.',
  },
  {
    field: 'webhook_url_recording_completed',
    label: 'Recording finished',
    description: 'Sent when the recording has downloaded successfully. If you also use commercial skip or re-encoding, that work may still be running.',
    tooltip: 'This fires as soon as the download itself is done, before any post-processing.',
  },
  {
    field: 'webhook_url_recording_failed',
    label: 'Recording failed',
    description: 'Sent when a recording could not be completed, for example the provider dropped the stream.',
    tooltip: 'A good place to point an alert so you find out about a missed show without checking the app.',
  },
  {
    field: 'webhook_url_recording_cancelled',
    label: 'Recording cancelled',
    description: 'Sent when you or someone else stops a recording before it finishes.',
    tooltip: 'Fires for a manual cancel, not for a recording that failed on its own.',
  },
  {
    field: 'webhook_url_postprocessing_completed',
    label: 'Processing finished',
    description: 'Sent once commercial skip and re-encoding are done and the final file is in your completed folder.',
    tooltip: 'This is usually the one to use for telling Plex, Jellyfin, or Emby to scan for the new file.',
  },
]

function parsedHost(url) {
  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return { error: 'scheme' }
    if (!parsed.hostname) return { error: 'host' }
    return { hostname: parsed.hostname.replace(/^\[|\]$/g, '') }
  } catch {
    return { error: 'host' }
  }
}

// Mirrors the address ranges the server refuses. Private and loopback
// addresses are deliberately fine: a box on your own network is the point.
function isForbiddenAddress(hostname) {
  const ipv4 = hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/)
  if (ipv4) {
    const octets = ipv4.slice(1).map(Number)
    if (octets.some((octet) => octet > 255)) return false
    const [a, b] = octets
    if (octets.every((octet) => octet === 0)) return true            // unspecified
    if (a === 169 && b === 254) return true                          // link-local
    if (a >= 224 && a <= 239) return true                            // multicast
    if (a >= 240) return true                                        // reserved
    return false
  }
  if (!hostname.includes(':')) return false
  const compact = hostname.toLowerCase()
  if (compact === '::') return true                                  // unspecified
  if (/^fe[89ab]/.test(compact)) return true                         // link-local
  if (/^ff/.test(compact)) return true                               // multicast
  return false
}

export function getWebhookErrors(formData) {
  if (!formData) return {}
  const errors = {}
  WEBHOOK_FIELDS.forEach(({ field }) => {
    const raw = formData[field]
    const value = typeof raw === 'string' ? raw.trim() : ''
    if (!value) return
    if (value.length > MAX_WEBHOOK_URL_LENGTH) {
      errors[field] = `Web address must be ${MAX_WEBHOOK_URL_LENGTH} characters or fewer.`
      return
    }
    if (/\s/.test(value)) {
      errors[field] = 'Web address cannot contain spaces.'
      return
    }
    const { error, hostname } = parsedHost(value)
    if (error === 'scheme') {
      errors[field] = 'Web address must start with http:// or https://'
      return
    }
    if (error === 'host' || !hostname) {
      errors[field] = 'Web address must include a host, for example http://192.168.1.10:8080/hook'
      return
    }
    if (isForbiddenAddress(hostname)) {
      errors[field] = 'That address is reserved and cannot receive a webhook.'
    }
  })
  return errors
}

export default function WebhooksSection({ formData, onChange }) {
  const errors = getWebhookErrors(formData)

  return (
    <Stack gap="xl">
      <SectionHeader
        title="Webhooks"
        description="Tell another app when a recording happens"
      />

      <Text size="sm" c="dimmed" maw="70ch">
        Paste a web address next to any event below and Mustarrd will send it a short JSON message when
        that event happens. Leave a box empty to turn that one off. An address on your own network is
        fine, so you can point these at Plex, Jellyfin, Home Assistant, or anything else in the house.
        If a message cannot be delivered it is written to the log and nothing else happens, so a webhook
        can never delay or break a recording.
      </Text>

      <SubGroup label="Events">
        <Stack gap="lg">
          {WEBHOOK_FIELDS.map(({ field, label, description, tooltip }) => (
            <TextInput
              key={field}
              aria-label={label}
              label={<LabelWithTooltip label={label} tooltip={tooltip} />}
              description={description}
              placeholder="http://192.168.1.10:8080/my-hook"
              value={formData?.[field] || ''}
              error={errors[field]}
              onChange={(e) => onChange(field, e.target.value)}
            />
          ))}
        </Stack>
      </SubGroup>
    </Stack>
  )
}
