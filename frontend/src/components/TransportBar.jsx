import { useState } from 'react'
import { ActionIcon, Group, Slider, Text } from '@mantine/core'
import {
  IconMaximize,
  IconPlayerPauseFilled,
  IconPlayerPlayFilled,
  IconVolume,
  IconVolumeOff,
} from '@tabler/icons-react'

export function formatTime(totalSeconds) {
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

// Playback controls for a stream the server is producing on the fly. The
// element's own controls cannot be used for these: the media element only
// knows about the portion FFmpeg has emitted so far, whereas `position` and
// `duration` here describe the whole recording or title. Seeking outside the
// produced portion is the parent's problem — it restarts the session — which
// is why this component only reports a target and never touches the video.
export default function TransportBar({
  position,
  duration,
  isPlaying,
  isMuted,
  onSeek,
  onTogglePlay,
  onToggleMute,
  onToggleFullscreen,
}) {
  // While a drag is in progress the slider follows the thumb rather than the
  // video, which is still playing at the old position underneath.
  const [scrubValue, setScrubValue] = useState(null)
  const shown = scrubValue ?? Math.min(position, duration || 0)

  return (
    <Group gap="sm" px="sm" py={8} wrap="nowrap" bg="dark.8" data-testid="transport-bar">
      <ActionIcon
        variant="subtle"
        color="gray.0"
        onClick={onTogglePlay}
        aria-label={isPlaying ? 'Pause' : 'Play'}
      >
        {isPlaying ? <IconPlayerPauseFilled size={18} /> : <IconPlayerPlayFilled size={18} />}
      </ActionIcon>
      <Text size="xs" c="gray.3" style={{ whiteSpace: 'nowrap', fontVariantNumeric: 'tabular-nums' }}>
        {formatTime(shown)} / {formatTime(duration)}
      </Text>
      <Slider
        style={{ flex: 1 }}
        size="sm"
        min={0}
        max={duration}
        step={1}
        value={shown}
        onChange={setScrubValue}
        onChangeEnd={(value) => {
          setScrubValue(null)
          onSeek(value)
        }}
        label={(value) => formatTime(value)}
        thumbLabel="Seek"
      />
      <ActionIcon
        variant="subtle"
        color="gray.0"
        onClick={onToggleMute}
        aria-label={isMuted ? 'Unmute' : 'Mute'}
      >
        {isMuted ? <IconVolumeOff size={18} /> : <IconVolume size={18} />}
      </ActionIcon>
      <ActionIcon
        variant="subtle"
        color="gray.0"
        onClick={onToggleFullscreen}
        aria-label="Fullscreen"
      >
        <IconMaximize size={18} />
      </ActionIcon>
    </Group>
  )
}
