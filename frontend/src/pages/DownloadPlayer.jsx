import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Group, Loader, Stack, Text, Title } from '@mantine/core'
import { IconAlertCircle, IconDownload } from '@tabler/icons-react'
import { downloadsApi } from '../api'
import { attachHls, attachMpegts, pickEngine } from '../utils/playbackEngine'

export default function DownloadPlayer() {
  const { downloadId } = useParams()
  const parsedId = Number(downloadId)
  const isValidId = Number.isInteger(parsedId) && parsedId > 0

  const videoRef = useRef(null)
  // Engine fallback chain: preferred engine first; native/mpegts failures
  // retry through the server-side HLS rendition before giving up.
  const [engine, setEngine] = useState(null)
  const [playbackError, setPlaybackError] = useState(null)
  // The server reaps idle HLS sessions (e.g. video paused for a long time);
  // allow one transparent restart that resumes from the same position.
  const [hlsAttempt, setHlsAttempt] = useState(0)
  const resumePositionRef = useRef(-1)

  const { data: download, isLoading, error: loadError } = useQuery({
    queryKey: ['download', parsedId],
    queryFn: () => downloadsApi.get(parsedId),
    enabled: isValidId,
  })

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
    return `/api/downloads/${parsedId}/hls/playlist.m3u8`
  }, [isValidId, parsedId])

  useEffect(() => {
    if (download?.output_path) {
      setEngine(pickEngine(download.output_path))
      setPlaybackError(null)
      setHlsAttempt(0)
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
        <video
          ref={videoRef}
          controls
          autoPlay
          playsInline
          preload="metadata"
          style={{ width: '100%', maxHeight: '70vh', background: '#000', borderRadius: 8 }}
          data-engine={engine || undefined}
        />
      )}
    </Stack>
  )
}
