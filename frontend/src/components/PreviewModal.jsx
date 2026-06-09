import { useMemo } from 'react'
import { Alert, Badge, Group, Modal, Stack, Text } from '@mantine/core'
import { IconAlertCircle } from '@tabler/icons-react'

export default function PreviewModal({ opened, onClose, program, channel, accountId, mode = 'catchup' }) {
  const previewUrl = useMemo(() => {
    if (!opened || !program || !accountId) return null
    const channelId = channel?.stream_id ?? program.channel_id
    if (channelId == null) return null

    const params = new URLSearchParams({ mode })
    if (mode === 'catchup') {
      if (!program.start_timestamp || !program.stop_timestamp) return null
      params.set('start_timestamp', String(program.start_timestamp))
      params.set('stop_timestamp', String(program.stop_timestamp))
      if (program.provider_start) {
        params.set('provider_start', program.provider_start)
      }
    }
    return `/api/accounts/${accountId}/channels/${encodeURIComponent(channelId)}/preview?${params.toString()}`
  }, [opened, program, channel, accountId, mode])

  return (
    <Modal opened={opened} onClose={onClose} title="Preview" size="lg" returnFocus={false}>
      <Stack gap="sm">
        <Group gap="xs" wrap="nowrap">
          <Stack gap={0} style={{ flex: 1, minWidth: 0 }}>
            <Text fw={600} truncate>
              {program?.title || 'Program'}
            </Text>
            {channel?.name && (
              <Text size="sm" c="dimmed" truncate>
                {channel.name}
              </Text>
            )}
          </Stack>
          <Badge variant="light" color={mode === 'live' ? 'red' : 'yellow'} style={{ flexShrink: 0 }}>
            {mode === 'live' ? 'Live' : 'Catchup'}
          </Badge>
        </Group>

        {previewUrl ? (
          <video
            controls
            autoPlay
            preload="none"
            style={{ width: '100%', maxHeight: '60vh', background: '#000', borderRadius: 8 }}
            src={previewUrl}
          />
        ) : (
          <Alert color="red" icon={<IconAlertCircle size={16} />}>
            Preview is not available for this program.
          </Alert>
        )}

        <Text size="xs" c="dimmed">
          Previews stream in the original broadcast format, so playback depends on your browser&apos;s
          codec support. If it stays black, downloading still works normally.
        </Text>
      </Stack>
    </Modal>
  )
}
