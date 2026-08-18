import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Badge, Group, Loader, Modal, Stack, Text } from '@mantine/core'
import { IconAlertCircle } from '@tabler/icons-react'

import { vodApi } from '../api'
import TransportBar from './TransportBar'
import { attachHls } from '../utils/playbackEngine'

const PREVIEW_LIMIT_MESSAGE =
  'Preview limit reached — close any other open preview and try again.'
const PREVIEW_FAILED_MESSAGE =
  'This title could not be prepared for playback. Downloading still works normally.'

// Preview a movie or an episode before committing to a download. There is no
// Direct path here, unlike a live preview: providers ship VOD as MKV, or as MP4
// with AC-3 audio, and no browser decodes either — so every VOD preview is
// converted on the server and the player never attempts a native <video src>.
export default function VodPreviewModal({
  opened,
  onClose,
  accountId,
  kind,
  itemId,
  seriesId = null,
  containerExtension = null,
  title,
  subtitle = null,
}) {
  // Callback ref rather than useRef: the Modal renders its children in a
  // portal after open, so a plain ref is still null when the attach effect
  // first runs and the effect would never retry.
  const [videoEl, setVideoEl] = useState(null)
  const [playbackError, setPlaybackError] = useState(null)
  const [converting, setConverting] = useState(false)
  // FFmpeg only produces the stream from `start` onwards, so every position
  // shown to the viewer is start + the element's own currentTime. Scrubbing
  // outside what this session has produced restarts it at the new offset.
  const [start, setStart] = useState(0)
  const [currentTime, setCurrentTime] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [isMuted, setIsMuted] = useState(false)
  const containerRef = useRef(null)

  const ready = Boolean(opened && accountId && kind && itemId)

  const { data: durationInfo } = useQuery({
    queryKey: ['vod-preview-duration', accountId, kind, itemId],
    queryFn: () => vodApi.previewDuration(accountId, kind, itemId, { seriesId, containerExtension }),
    enabled: ready,
    // The length of a film does not change while you are looking at it, and a
    // miss costs a provider round trip.
    staleTime: Infinity,
    retry: false,
  })
  const duration = durationInfo?.duration > 0 ? durationInfo.duration : null

  const playlistUrl = useMemo(() => {
    if (!ready) return null
    return vodApi.previewPlaylistUrl(accountId, kind, itemId, { containerExtension, start })
  }, [ready, accountId, kind, itemId, containerExtension, start])

  useEffect(() => {
    if (opened) return
    setStart(0)
    setCurrentTime(0)
    setPlaybackError(null)
    setConverting(false)
  }, [opened])

  useEffect(() => {
    if (!playlistUrl || !videoEl) return undefined
    setPlaybackError(null)
    setConverting(true)
    const clear = () => setConverting(false)
    videoEl.addEventListener('loadeddata', clear)
    videoEl.addEventListener('playing', clear)

    const destroy = attachHls(videoEl, playlistUrl, {
      onError: (_reason, data) => {
        setConverting(false)
        setPlaybackError(
          data?.response?.code === 429 ? PREVIEW_LIMIT_MESSAGE : PREVIEW_FAILED_MESSAGE
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
  }, [playlistUrl, videoEl])

  // Mirror the media element's state into the transport bar.
  useEffect(() => {
    if (!videoEl) return undefined
    const onTimeUpdate = () => setCurrentTime(videoEl.currentTime || 0)
    const onPlay = () => setIsPlaying(true)
    const onPause = () => setIsPlaying(false)
    const onVolumeChange = () => setIsMuted(videoEl.muted)
    videoEl.addEventListener('timeupdate', onTimeUpdate)
    videoEl.addEventListener('play', onPlay)
    videoEl.addEventListener('pause', onPause)
    videoEl.addEventListener('volumechange', onVolumeChange)
    setIsPlaying(!videoEl.paused)
    setIsMuted(videoEl.muted)
    return () => {
      videoEl.removeEventListener('timeupdate', onTimeUpdate)
      videoEl.removeEventListener('play', onPlay)
      videoEl.removeEventListener('pause', onPause)
      videoEl.removeEventListener('volumechange', onVolumeChange)
    }
  }, [videoEl])

  const seekTo = useCallback((target) => {
    if (!videoEl || duration == null) return
    const clamped = Math.min(Math.max(target, 0), duration)
    const relative = clamped - start
    const seekableEnd = videoEl.seekable?.length
      ? videoEl.seekable.end(videoEl.seekable.length - 1)
      : 0
    if (relative >= 0 && relative <= seekableEnd) {
      videoEl.currentTime = relative
      setCurrentTime(relative)
      return
    }
    // Outside what this session has produced: restart FFmpeg at the target.
    setCurrentTime(0)
    setStart(clamped)
  }, [videoEl, duration, start])

  const togglePlay = useCallback(() => {
    if (!videoEl) return
    if (videoEl.paused) {
      videoEl.play()?.catch(() => {})
    } else {
      videoEl.pause()
    }
  }, [videoEl])

  const toggleMute = useCallback(() => {
    if (videoEl) videoEl.muted = !videoEl.muted
  }, [videoEl])

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen()?.catch(() => {})
    } else {
      containerRef.current?.requestFullscreen()?.catch(() => {})
    }
  }, [])

  const showTransport = duration != null

  return (
    <Modal opened={opened} onClose={onClose} title="Preview" size="lg" returnFocus={false}>
      <Stack gap="sm">
        <Group gap="xs" wrap="nowrap">
          <Stack gap={0} style={{ flex: 1, minWidth: 0 }}>
            <Text fw={600} truncate>{title || 'Title'}</Text>
            {subtitle && (
              <Text size="sm" c="dimmed" truncate>{subtitle}</Text>
            )}
          </Stack>
          <Badge variant="light" color="grape" style={{ flexShrink: 0 }}>
            {kind === 'episode' ? 'Episode' : 'Movie'}
          </Badge>
        </Group>

        {playbackError && (
          <Alert color="red" icon={<IconAlertCircle size={16} />}>
            {playbackError}
          </Alert>
        )}

        {ready ? (
          <div
            ref={containerRef}
            style={{
              position: 'relative',
              display: 'flex',
              flexDirection: 'column',
              background: '#000',
              borderRadius: 8,
              overflow: 'hidden',
            }}
          >
            <video
              ref={setVideoEl}
              controls={!showTransport}
              autoPlay
              preload="none"
              playsInline
              onClick={showTransport ? togglePlay : undefined}
              style={{ width: '100%', maxHeight: '60vh', background: '#000', display: 'block' }}
            />
            {showTransport && (
              <TransportBar
                position={start + currentTime}
                duration={duration}
                isPlaying={isPlaying}
                isMuted={isMuted}
                onSeek={seekTo}
                onTogglePlay={togglePlay}
                onToggleMute={toggleMute}
                onToggleFullscreen={toggleFullscreen}
              />
            )}
            {converting && (
              <Group
                gap="xs"
                justify="center"
                style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}
              >
                <Loader size="sm" color="gray" />
                <Text size="sm" c="gray.3">This title needs converting, one moment</Text>
              </Group>
            )}
          </div>
        ) : (
          <Alert color="red" icon={<IconAlertCircle size={16} />}>
            Preview is not available for this title.
          </Alert>
        )}

        <Text size="xs" c="dimmed">
          Previews are converted on the server so your browser can play them, and stop after a few
          minutes. Downloads are always saved in the original format.
        </Text>
      </Stack>
    </Modal>
  )
}
