import { useEffect, useMemo, useState } from 'react'
import { Alert, Badge, Group, Modal, Stack, Text } from '@mantine/core'
import { IconAlertCircle } from '@tabler/icons-react'
import { attachMpegts, mpegtsSupported } from '../utils/playbackEngine'

export default function PreviewModal({ opened, onClose, program, channel, accountId, mode = 'catchup' }) {
  // Callback-ref state instead of useRef: the Modal renders its children in
  // a portal after open, so a plain ref is still null when the attach effect
  // first runs and the effect would never retry.
  const [videoEl, setVideoEl] = useState(null)
  const [playbackError, setPlaybackError] = useState(null)
  // The preview endpoint relays raw MPEG-TS, which no browser plays in a bare
  // <video>. With MSE we demux it client-side via mpegts.js; without MSE
  // (iOS Safari) we fall back to the plain tag and surface the limitation.
  const canDemuxTs = mpegtsSupported()

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

  useEffect(() => {
    setPlaybackError(null)
    if (!opened || !previewUrl || !videoEl || !canDemuxTs) return undefined
    const destroy = attachMpegts(videoEl, previewUrl, {
      live: true,
      onError: (_reason, info) =>
        setPlaybackError(
          info?.code === 429
            ? 'Preview limit reached — close any other open preview and try again.'
            : 'This stream uses a codec your browser cannot decode. Downloading still works normally.'
        ),
    })
    videoEl.play()?.catch(() => {})
    return destroy
  }, [opened, previewUrl, canDemuxTs, videoEl])

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

        {playbackError && (
          <Alert color="red" icon={<IconAlertCircle size={16} />}>
            {playbackError}
          </Alert>
        )}

        {previewUrl ? (
          <video
            ref={setVideoEl}
            controls
            autoPlay
            preload="none"
            playsInline
            style={{ width: '100%', maxHeight: '60vh', background: '#000', borderRadius: 8 }}
            src={canDemuxTs ? undefined : previewUrl}
          />
        ) : (
          <Alert color="red" icon={<IconAlertCircle size={16} />}>
            Preview is not available for this program.
          </Alert>
        )}

        <Text size="xs" c="dimmed">
          Previews stream in the original broadcast format. If playback fails, the codec is not
          supported by your browser &mdash; downloading still works normally.
        </Text>
      </Stack>
    </Modal>
  )
}
