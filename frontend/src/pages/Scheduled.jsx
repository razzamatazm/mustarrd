import { useEffect, useState } from 'react'
import {
  Title,
  Card,
  Group,
  Text,
  Stack,
  Badge,
  ActionIcon,
  Menu,
  Tabs,
  Loader,
  Alert,
  Button,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useNavigate } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  IconDotsVertical,
  IconPlayerStop,
  IconTrash,
  IconAlertCircle,
  IconCheck,
  IconX,
  IconClock,
  IconCalendar,
  IconDownload,
  IconSettings,
  IconPlayerPlay,
  IconFolderOpen,
} from '@tabler/icons-react'
import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'

import { accountsApi, schedulesApi } from '../api'
import { formatChannelDateTime, formatAirDateTime, getGuideOffsetHours } from '../utils/channelTime'

dayjs.extend(duration)

function formatDuration(minutes) {
  const d = dayjs.duration(minutes, 'minutes')
  const hours = d.hours()
  const mins = d.minutes()
  if (hours > 0) {
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
  }
  return `${mins}m`
}

function getStatusBadge(status) {
  const statusConfig = {
    scheduled: { color: 'gray', icon: IconCalendar, label: 'Scheduled' },
    queued: { color: 'yellow', icon: IconDownload, label: 'Queued' },
    downloading: { color: 'yellow', icon: IconDownload, label: 'Downloading' },
    processing: { color: 'teal', icon: IconSettings, label: 'Processing' },
    completed: { color: 'green', icon: IconCheck, label: 'Completed' },
    failed: { color: 'red', icon: IconX, label: 'Failed' },
    cancelled: { color: 'orange', icon: IconX, label: 'Cancelled' },
    paused_low_space: { color: 'yellow', icon: IconAlertCircle, label: 'Paused (Low Space)' },
  }

  const config = statusConfig[status] || statusConfig.scheduled
  const Icon = config.icon

  return (
    <Badge color={config.color} variant="light" leftSection={<Icon size={12} />}>
      {config.label}
    </Badge>
  )
}

function ScheduleCard({
  schedule,
  guideOffsetHours = 0,
  isDesktop,
  onCancel,
  onDelete,
  onOpenFileLocation,
  onPlayFile,
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const activeStatuses = ['scheduled', 'queued', 'downloading', 'processing', 'paused_low_space']
  const isActive = activeStatuses.includes(schedule.status)
  const canDelete = !isActive
  const startHasPassed = schedule.start_timestamp
    ? schedule.start_timestamp < Date.now() / 1000
    : false

  const startTime = formatAirDateTime(schedule, 'start', guideOffsetHours)
  const endTime = formatChannelDateTime(schedule, 'end', guideOffsetHours, 'h:mm A')
  const availableAt = formatAirDateTime(schedule, 'available', guideOffsetHours)
  const totalDuration = (schedule.duration_minutes || 0)
    + (schedule.pre_padding_minutes || 0)
    + (schedule.post_padding_minutes || 0)
  const downloadHref = schedule.download_id
    ? `/api/downloads/${schedule.download_id}/file?action=download`
    : null
  const playHref = schedule.download_id
    ? `/downloads/${schedule.download_id}/play`
    : null

  return (
    <Card shadow="sm" padding="md" radius="md" withBorder>
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap" align="flex-start">
          <Stack gap={2} style={{ flex: 1, overflow: 'hidden' }}>
            <Text fw={500}>
              {schedule.program_title}
            </Text>
            <Text size="sm" c="dimmed" truncate>
              {schedule.channel_name}
            </Text>
            {getStatusBadge(schedule.status)}
          </Stack>
          <Menu shadow="md" width={160}>
            <Menu.Target>
              <ActionIcon variant="subtle">
                <IconDotsVertical size={16} />
              </ActionIcon>
            </Menu.Target>
            <Menu.Dropdown>
              {isActive && (
                <Menu.Item
                  leftSection={<IconPlayerStop size={14} />}
                  onClick={() => onCancel(schedule)}
                >
                  Cancel
                </Menu.Item>
              )}
              {canDelete && (
                <Menu.Item
                  color="red"
                  leftSection={<IconTrash size={14} />}
                  onClick={() => setConfirmingDelete(true)}
                >
                  Delete
                </Menu.Item>
              )}
            </Menu.Dropdown>
          </Menu>
        </Group>

        <Text size="xs" c="dimmed">
          {(!isActive && startHasPassed) ? 'Aired' : 'Airs'}: {startTime || 'Unknown'} - {endTime || 'Unknown'} ({formatDuration(schedule.duration_minutes || 0)})
        </Text>

        {(isActive || schedule.status === 'completed') && (
          <Text size="xs" c="dimmed">
            Download starts: {availableAt || 'Unknown'} ({formatDuration(totalDuration)} recording)
          </Text>
        )}

        {schedule.status_message && (
          <Alert color="yellow" variant="light" p="xs">
            <Text size="xs">{schedule.status_message}</Text>
          </Alert>
        )}

        {!schedule.status_message && schedule.download_status === 'failed' && schedule.download_error_message && (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs">{schedule.download_error_message}</Text>
          </Alert>
        )}

        {schedule.download_status === 'completed' && schedule.download_id && (
          <Group gap="xs">
            {isDesktop ? (
              <>
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconFolderOpen size={14} />}
                  onClick={() => onOpenFileLocation(schedule)}
                >
                  Open File Location
                </Button>
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconPlayerPlay size={14} />}
                  onClick={() => onPlayFile(schedule)}
                >
                  Play
                </Button>
              </>
            ) : (
              <>
                <Button
                  component="a"
                  href={downloadHref}
                  size="xs"
                  variant="light"
                  leftSection={<IconDownload size={14} />}
                >
                  Download
                </Button>
                <Button
                  component="a"
                  href={playHref}
                  size="xs"
                  variant="default"
                  leftSection={<IconPlayerPlay size={14} />}
                >
                  Play
                </Button>
              </>
            )}
          </Group>
        )}

        {confirmingDelete && (
          <Group gap="xs" justify="flex-end" pt={4} style={{ borderTop: '1px solid var(--mantine-color-dark-4)' }}>
            <Text size="sm" c="dimmed">Delete this schedule?</Text>
            <Button
              size="xs"
              color="red"
              onClick={() => { onDelete(schedule); setConfirmingDelete(false) }}
            >
              Yes, delete
            </Button>
            <Button
              size="xs"
              variant="subtle"
              onClick={() => setConfirmingDelete(false)}
            >
              Cancel
            </Button>
          </Group>
        )}
      </Stack>
    </Card>
  )
}

export default function Scheduled() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [localItems, setLocalItems] = useState([])
  const desktopApi = typeof window !== 'undefined' ? window.mustarrdDesktop : null
  const isDesktop = Boolean(desktopApi?.openFileLocation && desktopApi?.playFile)

  const { data: schedules, isLoading, error } = useQuery({
    queryKey: ['schedules'],
    queryFn: schedulesApi.list,
    refetchInterval: 5000,
  })

  const { data: accounts } = useQuery({
    queryKey: ['accounts', 'public'],
    queryFn: accountsApi.publicList,
  })

  useEffect(() => {
    if (schedules) {
      setLocalItems(schedules)
    }
  }, [schedules])

  const cancelMutation = useMutation({
    mutationFn: (schedule) => schedulesApi.cancel(schedule.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      notifications.show({
        title: 'Schedule Cancelled',
        message: 'The scheduled recording has been cancelled',
        color: 'orange',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Error',
        message: error.message,
        color: 'red',
      })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (schedule) => schedulesApi.cancel(schedule.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      notifications.show({
        title: 'Schedule Deleted',
        message: 'The schedule has been removed',
        color: 'green',
      })
    },
  })

  const handleOpenFileLocation = async (schedule) => {
    if (!desktopApi?.openFileLocation) return
    if (!schedule.download_output_path) {
      notifications.show({
        title: 'Unable to Open File Location',
        message: 'No downloaded file path is available for this recording.',
        color: 'red',
      })
      return
    }
    try {
      const result = await desktopApi.openFileLocation(schedule.download_output_path)
      if (result?.success) return
      notifications.show({
        title: 'Unable to Open File Location',
        message: result?.error || 'The file location could not be opened.',
        color: 'red',
      })
    } catch (error) {
      notifications.show({
        title: 'Unable to Open File Location',
        message: error?.message || 'The file location could not be opened.',
        color: 'red',
      })
    }
  }

  const handlePlayFile = async (schedule) => {
    if (!desktopApi?.playFile) return
    if (!schedule.download_output_path) {
      notifications.show({
        title: 'Unable to Play File',
        message: 'No downloaded file path is available for this recording.',
        color: 'red',
      })
      return
    }
    try {
      const result = await desktopApi.playFile(schedule.download_output_path)
      if (result?.success) return
      notifications.show({
        title: 'Unable to Play File',
        message: result?.error || 'The file could not be opened for playback.',
        color: 'red',
      })
    } catch (error) {
      notifications.show({
        title: 'Unable to Play File',
        message: error?.message || 'The file could not be opened for playback.',
        color: 'red',
      })
    }
  }

  const activeSchedules = localItems?.filter((s) =>
    ['scheduled', 'queued', 'downloading', 'processing', 'paused_low_space'].includes(s.status)
  ) || []

  const historySchedules = localItems?.filter((s) =>
    ['completed', 'failed', 'cancelled'].includes(s.status)
  ) || []
  const accountGuideOffsets = Object.fromEntries(
    (accounts || []).map((account) => [Number(account.id), getGuideOffsetHours(account.guide_offset_hours)])
  )

  if (isLoading) {
    return (
      <Stack align="center" justify="center" h={300}>
        <Loader />
      </Stack>
    )
  }

  if (error) {
    return (
      <Alert icon={<IconAlertCircle size={16} />} title="Error" color="red">
        Failed to load schedules: {error.message}
      </Alert>
    )
  }

  return (
    <Stack>
      <Title order={2}>Scheduled Recordings</Title>

      <Tabs defaultValue="upcoming">
        <Tabs.List>
          <Tabs.Tab value="upcoming" leftSection={<IconCalendar size={16} />}>
            Upcoming {activeSchedules.length > 0 && `(${activeSchedules.length})`}
          </Tabs.Tab>
          <Tabs.Tab value="history" leftSection={<IconClock size={16} />}>
            History {historySchedules.length > 0 && `(${historySchedules.length})`}
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="upcoming" pt="md">
          {activeSchedules.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconCalendar size={48} opacity={0.3} />
                <Stack gap={4} align="center">
                  <Text c="dimmed" ta="center">
                    No scheduled recordings yet.
                  </Text>
                  <Text size="sm" c="dimmed" ta="center" maw={400}>
                    Browse the program guide to find upcoming shows, then click "Schedule" on any program to record it automatically when it airs.
                  </Text>
                </Stack>
                <Button onClick={() => navigate('/browse')}>
                  Go to Browse
                </Button>
              </Stack>
            </Card>
          ) : (
            <Stack gap="md">
              {activeSchedules.map((schedule) => (
                <ScheduleCard
                  key={schedule.id}
                  schedule={schedule}
                  guideOffsetHours={accountGuideOffsets[Number(schedule.account_id)] || 0}
                  isDesktop={isDesktop}
                  onCancel={(s) => cancelMutation.mutate(s)}
                  onDelete={(s) => deleteMutation.mutate(s)}
                  onOpenFileLocation={handleOpenFileLocation}
                  onPlayFile={handlePlayFile}
                />
              ))}
            </Stack>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="md">
          {historySchedules.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconClock size={48} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No schedule history yet.
                </Text>
                <Text c="dimmed" ta="center" size="sm">
                  Completed, failed, and cancelled scheduled recordings will appear here.
                </Text>
              </Stack>
            </Card>
          ) : (
            <Stack gap="md">
              {historySchedules.map((schedule) => (
                <ScheduleCard
                  key={schedule.id}
                  schedule={schedule}
                  guideOffsetHours={accountGuideOffsets[Number(schedule.account_id)] || 0}
                  isDesktop={isDesktop}
                  onCancel={(s) => cancelMutation.mutate(s)}
                  onDelete={(s) => deleteMutation.mutate(s)}
                  onOpenFileLocation={handleOpenFileLocation}
                  onPlayFile={handlePlayFile}
                />
              ))}
            </Stack>
          )}
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}
