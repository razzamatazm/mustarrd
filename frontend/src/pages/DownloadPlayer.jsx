import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ActionIcon, Alert, Button, Group, Loader, Slider, Stack, Text, Title } from '@mantine/core'
import {
  IconAlertCircle,
  IconDownload,
  IconMaximize,
  IconPlayerPauseFilled,
  IconPlayerPlayFilled,
  IconVolume,
  IconVolumeOff,
} from '@tabler/icons-react'
import { downloadsApi } from '../api'
import { attachHls, attachMpegts, pickEngine } from '../utils/playbackEngine'

function formatTime(totalSeconds) {
  if (!Number.isFinite(totalSeconds) || totalSeconds < 0) return '0:00'
  const s = Math.floor(totalSeconds)
  const hours = Math.floor(s / 3600)
  const minutes = Math.floor((s % 3600) / 60)
  const seconds = s % 60
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
  }
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

export default function DownloadPlayer() {
  const { downloadId } = useParams()
  const parsedId = Number(downloadId)
  const isValidId = Number.isInteger(parsedId) && parsedId > 0

  const videoRef = useRef(null)
  const containerRef = useRef(null)
  // Engine fallback chain: preferred engine first; native/mpegts failures
  // retry through the server-side HLS rendition before giving up.
  const [engine, setEngine] = useState(null)
  const [playbackError, setPlaybackError] = useState(null)
  // The server reaps idle HLS sessions (e.g. video paused for a long time);
  // allow one transparent restart that resumes from the same position.
  const [hlsAttempt, setHlsAttempt] = useState(0)
  const resumePositionRef = useRef(-1)
  // The HLS rendition is produced on the fly, so the playlist only covers
  // what FFmpeg has emitted so far. Scrubbing past that point restarts the
  // session at the target offset; hlsStart is that offset, and all timeline
  // positions shown to the user are hlsStart + the element's currentTime.
  const [hlsStart, setHlsStart] = useState(0)
  const [currentTime, setCurrentTime] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [isMuted, setIsMuted] = useState(false)
  const [scrubValue, setScrubValue] = useState(null)

  const { data: download, isLoading, error: loadError } = useQuery({
    queryKey: ['download', parsedId],
    queryFn: () => downloadsApi.get(parsedId),
    enabled: isValidId,
  })

  const { data: playbackInfo } = useQuery({
    queryKey: ['download-playback-info', parsedId],
    queryFn: () => downloadsApi.playbackInfo(parsedId),
    enabled: isValidId && engine === 'hls',
    staleTime: Infinity,
  })
  const fullDuration = playbackInfo?.duration > 0 ? playbackInfo.duration : null
  // Custom transport bar only makes sense for the HLS rendition (the other
  // engines play the finished file, where native controls already work).
  const customControls = engine === 'hls' && fullDuration != null

  const playUrl = useMemo(() => {
    if (!isValidId) return null
    return `/api/downloads/${parsedId}/file?action=play`
  }, [isValidId, parsedId])

  const downloadUrl = useMemo(() => {
    if (!isValidId) return null
    return `/api/downloads/${parsedId}/file?action=download`
  }, [isValidId, parsedId])

  const hlsUrl = useMemo(() => {
    if (!isValidId) return null
    const base = `/api/downloads/${parsedId}/hls/playlist.m3u8`
    return hlsStart > 0 ? `${base}?start=${hlsStart.toFixed(3)}` : base
  }, [isValidId, parsedId, hlsStart])

  useEffect(() => {
    if (download?.output_path) {
      setEngine(pickEngine(download.output_path))
      setPlaybackError(null)
      setHlsAttempt(0)
      setHlsStart(0)
      setCurrentTime(0)
      resumePositionRef.current = -1
    }
  }, [download?.output_path])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !engine || !playUrl) return undefined

    const fallbackToHls = (reason) => {
      if (engine === 'hls') {
        setPlaybackError(reason)
      } else {
        setEngine('hls')
      }
    }

    if (engine === 'mpegts') {
      return attachMpegts(video, playUrl, { live: false, onError: fallbackToHls })
    }
    if (engine === 'hls') {
      return attachHls(video, hlsUrl, {
        startPosition: resumePositionRef.current,
        onError: (reason) => {
          if (hlsAttempt === 0) {
            resumePositionRef.current = video.currentTime || -1
            setHlsAttempt(1)
          } else {
            setPlaybackError(reason)
          }
        },
      })
    }
    // Native: plain src; a media error means the codec/container lost, so
    // hand it to the HLS pipeline.
    video.src = playUrl
    const onMediaError = () => fallbackToHls('native playback failed')
    video.addEventListener('error', onMediaError)
    return () => {
      video.removeEventListener('error', onMediaError)
      video.removeAttribute('src')
      video.load()
    }
  }, [engine, playUrl, hlsUrl, hlsAttempt])

  // Mirror the media element's state into the custom transport bar.
  useEffect(() => {
    const video = videoRef.current
    if (!video || !customControls) return undefined
    const onTimeUpdate = () => setCurrentTime(video.currentTime || 0)
    const onPlay = () => setIsPlaying(true)
    const onPause = () => setIsPlaying(false)
    const onVolumeChange = () => setIsMuted(video.muted)
    video.addEventListener('timeupdate', onTimeUpdate)
    video.addEventListener('play', onPlay)
    video.addEventListener('pause', onPause)
    video.addEventListener('volumechange', onVolumeChange)
    setIsPlaying(!video.paused)
    setIsMuted(video.muted)
    return () => {
      video.removeEventListener('timeupdate', onTimeUpdate)
      video.removeEventListener('play', onPlay)
      video.removeEventListener('pause', onPause)
      video.removeEventListener('volumechange', onVolumeChange)
    }
  }, [customControls])

  const seekTo = useCallback((target) => {
    const video = videoRef.current
    if (!video || fullDuration == null) return
    const clamped = Math.min(Math.max(target, 0), fullDuration)
    const relative = clamped - hlsStart
    const seekableEnd = video.seekable?.length
      ? video.seekable.end(video.seekable.length - 1)
      : 0
    if (relative >= 0 && relative <= seekableEnd) {
      video.currentTime = relative
      setCurrentTime(relative)
      return
    }
    // Outside what this session has produced: restart FFmpeg at the target.
    resumePositionRef.current = -1
    setCurrentTime(0)
    setHlsStart(clamped)
  }, [fullDuration, hlsStart])

  const togglePlay = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      video.play()?.catch(() => {})
    } else {
      video.pause()
    }
  }, [])

  const toggleMute = useCallback(() => {
    const video = videoRef.current
    if (video) video.muted = !video.muted
  }, [])

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen()?.catch(() => {})
    } else {
      containerRef.current?.requestFullscreen()?.catch(() => {})
    }
  }, [])

  const timelinePosition = scrubValue ?? Math.min(hlsStart + currentTime, fullDuration || 0)

  if (!isValidId) {
    return (
      <Stack gap="md">
        <Title order={3}>Playback</Title>
        <Alert color="red" icon={<IconAlertCircle size={16} />}>
          Invalid download id.
        </Alert>
        <Group>
          <Button component={Link} to="/downloads" variant="default">Back to Downloads</Button>
        </Group>
      </Stack>
    )
  }

  return (
    <Stack gap="md">
      <Group justify="space-between" align="center">
        <Stack gap={0}>
          <Title order={3}>Playback</Title>
          {download?.program_title && (
            <Text size="sm" c="dimmed">{download.program_title}</Text>
          )}
        </Stack>
        <Group>
          <Button component={Link} to="/downloads" variant="default">Back to Downloads</Button>
          <Button
            component="a"
            href={downloadUrl}
            leftSection={<IconDownload size={14} />}
            variant="light"
          >
            Download File
          </Button>
        </Group>
      </Group>

      {loadError && (
        <Alert color="red" icon={<IconAlertCircle size={16} />}>
          Could not load this recording: {loadError.message}
        </Alert>
      )}

      {playbackError && (
        <Alert color="red" icon={<IconAlertCircle size={16} />} title="Playback failed">
          {String(playbackError)}. You can still use Download File to watch it in a media player.
        </Alert>
      )}

      {isLoading ? (
        <Group justify="center" py="xl"><Loader /></Group>
      ) : (
        <div
          ref={containerRef}
          style={{
            display: 'flex',
            flexDirection: 'column',
            background: '#000',
            borderRadius: 8,
            overflow: 'hidden',
          }}
        >
          <video
            ref={videoRef}
            controls={!customControls}
            autoPlay
            playsInline
            preload="metadata"
            onClick={customControls ? togglePlay : undefined}
            style={{ width: '100%', maxHeight: '70vh', background: '#000', flex: 1, minHeight: 0 }}
            data-engine={engine || undefined}
          />
          {customControls && (
            <Group gap="sm" px="sm" py={8} wrap="nowrap" bg="dark.8" data-testid="transport-bar">
              <ActionIcon
                variant="subtle"
                color="gray.0"
                onClick={togglePlay}
                aria-label={isPlaying ? 'Pause' : 'Play'}
              >
                {isPlaying ? <IconPlayerPauseFilled size={18} /> : <IconPlayerPlayFilled size={18} />}
              </ActionIcon>
              <Text size="xs" c="gray.3" style={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
                {formatTime(timelinePosition)} / {formatTime(fullDuration)}
              </Text>
              <Slider
                style={{ flex: 1 }}
                size="sm"
                min={0}
                max={fullDuration}
                step={1}
                value={timelinePosition}
                onChange={setScrubValue}
                onChangeEnd={(value) => {
                  setScrubValue(null)
                  seekTo(value)
                }}
                label={(value) => formatTime(value)}
                thumbLabel="Seek"
              />
              <ActionIcon
                variant="subtle"
                color="gray.0"
                onClick={toggleMute}
                aria-label={isMuted ? 'Unmute' : 'Mute'}
              >
                {isMuted ? <IconVolumeOff size={18} /> : <IconVolume size={18} />}
              </ActionIcon>
              <ActionIcon
                variant="subtle"
                color="gray.0"
                onClick={toggleFullscreen}
                aria-label="Fullscreen"
              >
                <IconMaximize size={18} />
              </ActionIcon>
            </Group>
          )}
        </div>
      )}
    </Stack>
  )
}
