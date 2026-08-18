import { useState, useEffect } from 'react'
import {
  Modal,
  Stack,
  Text,
  TextInput,
  Button,
  Group,
  Badge,
  Alert,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { IconClock, IconAlertTriangle } from '@tabler/icons-react'
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc.js'

import { timeSlotsApi } from '../api'
import {
  addMinutesToChannelInput,
  channelDisplayToUnix,
  getGuideOffsetHours,
  nowChannelInput,
} from '../utils/channelTime'

dayjs.extend(utc)

// Kept in step with the backend bounds in api/time_slots.py.
export const MAX_TIME_SLOT_MINUTES = 6 * 60
export const MAX_FUTURE_DAYS = 14
const DEFAULT_ARCHIVE_DAYS = 7

export function suggestTitle(channelName, startValue) {
  const parsed = dayjs.utc(startValue)
  if (!channelName || !parsed.isValid()) {
    return channelName || ''
  }
  return `${channelName} - ${parsed.format('MMM D h:mm A')}`
}

export function validateTimeSlot({ startValue, endValue, archiveDays, guideOffsetHours = 0 }) {
  const startTs = channelDisplayToUnix(startValue, guideOffsetHours)
  const stopTs = channelDisplayToUnix(endValue, guideOffsetHours)

  if (startTs == null || stopTs == null) {
    return 'Enter a start and an end time.'
  }
  if (stopTs <= startTs) {
    return 'The end time must be after the start time.'
  }

  const durationMinutes = Math.round((stopTs - startTs) / 60)
  if (durationMinutes > MAX_TIME_SLOT_MINUTES) {
    return `A time slot can be at most ${MAX_TIME_SLOT_MINUTES / 60} hours long.`
  }

  const days = archiveDays && archiveDays > 0 ? archiveDays : DEFAULT_ARCHIVE_DAYS
  const nowTs = dayjs.utc().unix()
  if (startTs < nowTs - days * 86400) {
    return `That start time is outside this channel's catchup window. This channel keeps ${days} days of catchup.`
  }
  if (startTs > nowTs + MAX_FUTURE_DAYS * 86400) {
    return `A time slot can start at most ${MAX_FUTURE_DAYS} days in the future.`
  }

  return null
}

function formatDuration(minutes) {
  if (!Number.isFinite(minutes) || minutes <= 0) {
    return ''
  }
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  if (!hours) return `${mins}m`
  if (!mins) return `${hours}h`
  return `${hours}h ${mins}m`
}

export default function TimeSlotModal({
  opened,
  onClose,
  channel,
  accountId,
  guideOffsetHours = 0,
  archiveDays = null,
}) {
  const queryClient = useQueryClient()
  const normalizedGuideOffsetHours = getGuideOffsetHours(guideOffsetHours)
  const [startValue, setStartValue] = useState('')
  const [endValue, setEndValue] = useState('')
  const [title, setTitle] = useState('')
  const [titleDirty, setTitleDirty] = useState(false)
  const [overlapWarning, setOverlapWarning] = useState(null)

  useEffect(() => {
    if (!opened || !channel) {
      return
    }
    const start = nowChannelInput(normalizedGuideOffsetHours, 15)
    setStartValue(start)
    setEndValue(addMinutesToChannelInput(start, 60))
    setTitle(suggestTitle(channel.name, start))
    setTitleDirty(false)
    setOverlapWarning(null)
  }, [opened, channel, normalizedGuideOffsetHours])

  // Keep the suggestion in step with the start time until the user takes over.
  useEffect(() => {
    if (!opened || titleDirty || !channel) {
      return
    }
    setTitle(suggestTitle(channel.name, startValue))
  }, [opened, titleDirty, channel, startValue])

  const createMutation = useMutation({
    mutationFn: (data) => timeSlotsApi.create(data),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      notifications.show({
        title: result?.kind === 'download' ? 'Recording queued' : 'Recording scheduled',
        message:
          result?.kind === 'download'
            ? 'That time slot is already in the catchup archive, so it is downloading now.'
            : 'That time slot has not aired yet. It will download once your provider publishes it.',
        color: 'green',
      })
      onClose()
    },
    onError: (error) => {
      if (error?.status === 409 && error?.payload?.detail?.code === 'overlap') {
        setOverlapWarning(error.payload.detail.message)
        return
      }
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })

  if (!channel) {
    return null
  }

  const validationError = validateTimeSlot({
    startValue,
    endValue,
    archiveDays,
    guideOffsetHours: normalizedGuideOffsetHours,
  })

  const startTs = channelDisplayToUnix(startValue, normalizedGuideOffsetHours)
  const stopTs = channelDisplayToUnix(endValue, normalizedGuideOffsetHours)
  const durationMinutes =
    startTs != null && stopTs != null && stopTs > startTs ? Math.round((stopTs - startTs) / 60) : 0
  const willWait = stopTs != null && stopTs > dayjs.utc().unix()
  const trimmedTitle = title.trim()
  const canSubmit = !validationError && !!trimmedTitle

  const submit = (confirmOverlap = false) => {
    createMutation.mutate({
      account_id: parseInt(accountId),
      channel_id: channel.stream_id.toString(),
      channel_name: channel.name,
      title: trimmedTitle,
      start_timestamp: startTs,
      stop_timestamp: stopTs,
      confirm_overlap: confirmOverlap,
    })
  }

  return (
    <Modal opened={opened} onClose={onClose} title="Record a time slot" size="lg" returnFocus={false}>
      <Stack>
        <Stack gap={2}>
          <Text fw={600} size="lg">{channel.name}</Text>
          <Text size="sm" c="dimmed">
            Record any stretch of this channel, whether or not the guide lists it.
          </Text>
        </Stack>

        <Group grow align="flex-start">
          <TextInput
            type="datetime-local"
            label="Start"
            description="On this channel's guide clock"
            value={startValue}
            onChange={(e) => setStartValue(e.target.value)}
          />
          <TextInput
            type="datetime-local"
            label="End"
            description={`At most ${MAX_TIME_SLOT_MINUTES / 60} hours after the start`}
            value={endValue}
            onChange={(e) => setEndValue(e.target.value)}
          />
        </Group>

        {durationMinutes > 0 && !validationError && (
          <Group gap="xs">
            <Badge variant="light" leftSection={<IconClock size={12} />}>
              {formatDuration(durationMinutes)}
            </Badge>
            <Badge variant="light" color={willWait ? 'yellow' : 'blue'}>
              {willWait ? 'Waits for catchup' : 'Downloads now'}
            </Badge>
          </Group>
        )}

        <TextInput
          label="Name"
          description="Used for the recording and its filename"
          value={title}
          onChange={(e) => {
            setTitleDirty(true)
            setTitle(e.target.value)
          }}
        />

        <Alert variant="light" color="gray">
          <Text size="sm">
            Records exactly the times you enter. Unlike guide recordings, no extra minutes are
            added before or after.
          </Text>
        </Alert>

        {willWait && !validationError && (
          <Text size="sm" c="dimmed">
            This has not aired yet, so it will wait until your provider adds it to the catchup
            archive and then download. It does not record the live stream.
          </Text>
        )}

        {validationError && (
          <Alert variant="light" color="red">
            <Text size="sm">{validationError}</Text>
          </Alert>
        )}

        {overlapWarning && (
          <Alert variant="light" color="yellow" icon={<IconAlertTriangle size={16} />}>
            <Text size="sm">{overlapWarning} Record it anyway?</Text>
          </Alert>
        )}

        <Group justify="flex-end" mt="md">
          <Button variant="subtle" onClick={onClose}>Cancel</Button>
          {overlapWarning ? (
            <Button
              color="yellow"
              onClick={() => submit(true)}
              loading={createMutation.isPending}
              disabled={!canSubmit}
            >
              Record anyway
            </Button>
          ) : (
            <Button
              onClick={() => submit(false)}
              loading={createMutation.isPending}
              disabled={!canSubmit}
            >
              Record
            </Button>
          )}
        </Group>
      </Stack>
    </Modal>
  )
}
