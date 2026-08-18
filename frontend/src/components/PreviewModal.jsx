import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Badge, Group, Loader, Modal, Stack, Text } from '@mantine/core'
import { IconAlertCircle } from '@tabler/icons-react'
import {
  attachHls,
  attachMpegts,
  mpegtsSupported,
  previewNeedsConversion,
  rememberPreviewNeedsConversion,
} from '../utils/playbackEngine'

// How long a Direct preview gets to show something before we assume the
// browser cannot decode this stream. mpegts.js does not always raise an error
// on an undecodable audio track — AC-3 in particular can stall silently — so
// the watchdog catches the failure that most looks like a broken app.
const DIRECT_WATCHDOG_MS = 6000

const PREVIEW_LIMIT_MESSAGE =
  'Preview limit reached — close any other open preview and try again.'
// mpegts.js reports the provider being unreachable separately from the
// browser being unable to decode. Converting cannot fix the former, and
// remembering it would pin a perfectly playable channel to FFmpeg forever.
const NETWORK_ERROR_PREFIX = 'NetworkError'

export default function PreviewModal({ opened, onClose, program, channel, accountId, mode = 'catchup' }) {
  // Callback-ref state instead of useRef: the Modal renders its children in
  // a portal after open, so a plain ref is still null when the attach effect
  // first runs and the effect would never retry.
  const [videoEl, setVideoEl] = useState(null)
  const [playbackError, setPlaybackError] = useState(null)
  // 'direct' — the browser decodes the provider's original stream.
  // 'converted' — the backend repackages it with FFmpeg first.
  const [engine, setEngine] = useState(null)
  const [converting, setConverting] = useState(false)
  // Without MSE (iOS Safari) there is no client-side demuxer at all, so the
  // Direct path cannot work for any channel.
  const canDemuxTs = mpegtsSupported()

  const channelId = channel?.stream_id ?? program?.channel_id

  const previewParams = useMemo(() => {
    if (!opened || !program || !accountId || channelId == null) return null
    const params = new URLSearchParams({ mode })
    if (mode === 'catchup') {
      if (!program.start_timestamp || !program.stop_timestamp) return null
      params.set('start_timestamp', String(program.start_timestamp))
      params.set('stop_timestamp', String(program.stop_timestamp))
      if (program.provider_start) {
        params.set('provider_start', program.provider_start)
      }
    }
    return params.toString()
  }, [opened, program, channelId, accountId, mode])

  const previewBase = channelId == null
    ? null
    : `/api/accounts/${accountId}/channels/${encodeURIComponent(channelId)}/preview`
  const directUrl = previewParams ? `${previewBase}?${previewParams}` : null
  const convertedUrl = previewParams ? `${previewBase}/hls/playlist.m3u8?${previewParams}` : null

  // Pick the starting engine each time the modal opens. A channel that has
  // already failed here goes straight to Converted rather than paying for a
  // second failed attempt.
  useEffect(() => {
    if (!opened) {
      setEngine(null)
      setConverting(false)
      setPlaybackError(null)
      return
    }
    setEngine(
      !canDemuxTs || previewNeedsConversion(accountId, channelId) ? 'converted' : 'direct'
    )
  }, [opened, canDemuxTs, accountId, channelId])

  const fallBackToConverted = useCallback(() => {
    rememberPreviewNeedsConversion(accountId, channelId)
    setPlaybackError(null)
    setEngine((current) => (current === 'direct' ? 'converted' : current))
  }, [accountId, channelId])

  // Direct: mpegts.js demuxes the raw MPEG-TS relay into MSE.
  useEffect(() => {
    if (engine !== 'direct' || !directUrl || !videoEl) return undefined
    setPlaybackError(null)
    const startedAt = videoEl.currentTime
    // Declared before attaching: mpegts.js can report an error during load,
    // and the handler below clears this timer.
    let watchdog = null

    const giveUp = (message) => {
      clearTimeout(watchdog)
      setPlaybackError(message)
    }

    const destroy = attachMpegts(videoEl, directUrl, {
      live: true,
      onError: (reason, info) => {
        if (info?.code === 429) {
          // Converting shares the same budget, so it would be refused too.
          giveUp(PREVIEW_LIMIT_MESSAGE)
        } else if (String(reason).startsWith(NETWORK_ERROR_PREFIX)) {
          giveUp('The provider could not be reached for this preview. Downloading may still work.')
        } else {
          fallBackToConverted()
        }
      },
    })
    videoEl.play()?.catch(() => {})

    watchdog = setTimeout(() => {
      // Autoplay blocked by the browser leaves the video paused with data
      // buffered; that is not a decode failure, so only a stalled *playing*
      // video or a video with nothing buffered at all counts.
      const stalled = videoEl.readyState < 2 || (!videoEl.paused && videoEl.currentTime <= startedAt)
      if (stalled) fallBackToConverted()
    }, DIRECT_WATCHDOG_MS)

    return () => {
      clearTimeout(watchdog)
      destroy()
    }
  }, [engine, directUrl, videoEl, fallBackToConverted])

  // Converted: the backend repackages the stream into fMP4 HLS.
  useEffect(() => {
    if (engine !== 'converted' || !convertedUrl || !videoEl) return undefined
    setPlaybackError(null)
    setConverting(true)
    const clear = () => setConverting(false)
    videoEl.addEventListener('loadeddata', clear)
    videoEl.addEventListener('playing', clear)

    const destroy = attachHls(videoEl, convertedUrl, {
      onError: (_reason, data) => {
        setConverting(false)
        setPlaybackError(
          data?.response?.code === 429
            ? PREVIEW_LIMIT_MESSAGE
            : 'This stream could not be prepared for playback. Downloading still works normally.'
        )
      },
    })
    videoEl.play()?.catch(() => {})

    return () => {
      videoEl.removeEventListener('loadeddata', clear)
      videoEl.removeEventListener('playing', clear)
      setConverting(false)
      destroy()
    }
  }, [engine, convertedUrl, videoEl])

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

        {directUrl ? (
          <div style={{ position: 'relative' }}>
            <video
              ref={setVideoEl}
              controls
              autoPlay
              preload="none"
              playsInline
              style={{ width: '100%', maxHeight: '60vh', background: '#000', borderRadius: 8, display: 'block' }}
            />
            {converting && (
              <Group
                gap="xs"
                justify="center"
                style={{
                  position: 'absolute',
                  inset: 0,
                  pointerEvents: 'none',
                }}
              >
                <Loader size="sm" color="gray" />
                <Text size="sm" c="gray.3">
                  This channel needs converting, one moment
                </Text>
              </Group>
            )}
          </div>
        ) : (
          <Alert color="red" icon={<IconAlertCircle size={16} />}>
            Preview is not available for this program.
          </Alert>
        )}

        <Text size="xs" c="dimmed">
          Previews stream in the original broadcast format where your browser can play it, and are
          converted on the server when it cannot. Downloads are always saved in the original format.
        </Text>
      </Stack>
    </Modal>
  )
}
