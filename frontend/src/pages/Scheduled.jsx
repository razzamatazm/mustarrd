import { useEffect, useRef, useState } from 'react'
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
  Select,
  Switch,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useNavigate, Link, useSearchParams } from 'react-router-dom'
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
  IconFilter,
  IconRefresh,
  IconFileExport,
  IconFileImport,
} from '@tabler/icons-react'
import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'

import { accountsApi, authApi, downloadsApi, schedulesApi, settingsApi } from '../api'
import { formatChannelDateTime, formatAirDateTime, getGuideOffsetHours } from '../utils/channelTime'

dayjs.extend(duration)

const ACCOUNT_SETTINGS_SUFFIX = 'your account settings.'

function renderErrorMessage(msg) {
  if (!msg) return null
  const idx = msg.indexOf(ACCOUNT_SETTINGS_SUFFIX)
  if (idx === -1) return msg
  return (
    <>
      {msg.slice(0, idx)}
      <Link to="/settings?section=accounts" style={{ color: 'var(--mantine-color-orange-5)' }}>
        your account settings
      </Link>
      .
    </>
  )
}

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
  onRetryDownload,
}) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const activeStatuses = ['scheduled', 'queued', 'downloading', 'processing', 'paused_low_space']
  const terminalStatuses = ['completed', 'failed', 'cancelled']
  const isActive = activeStatuses.includes(schedule.status)
  const isTerminal = terminalStatuses.includes(schedule.status)
  const canDelete = !isActive
  const startHasPassed = isTerminal
    ? true
    : (schedule.start_timestamp ? schedule.start_timestamp < Date.now() / 1000 : false)

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
            Download starts: {availableAt || 'Unknown'}{totalDuration !== (schedule.duration_minutes || 0) ? ` (${formatDuration(totalDuration)} with padding)` : ''}
          </Text>
        )}

        {schedule.status_message && (
          <Alert color="yellow" variant="light" p="xs">
            <Text size="xs">{schedule.status_message}</Text>
          </Alert>
        )}

        {!schedule.status_message && schedule.download_status === 'failed' && schedule.download_error_message && (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs">{renderErrorMessage(schedule.download_error_message)}</Text>
          </Alert>
        )}
        {schedule.download_status === 'failed' && schedule.download_id && (
          <Group>
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} />}
              onClick={() => onRetryDownload(schedule)}
            >
              Retry
            </Button>
          </Group>
        )}

        {schedule.status === 'cancelled' && !schedule.status_message && !schedule.download_id && (
          <Text size="xs" c="dimmed">
            Cancelled before downloading. If this program is still in your provider&apos;s catchup window, you can find it in{' '}
            <Link to="/browse" style={{ color: 'var(--mantine-color-orange-5)' }}>Browse EPG</Link>.
          </Text>
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
  const [searchParams, setSearchParams] = useSearchParams()
  const [localItems, setLocalItems] = useState([])
  const [historyFilter, setHistoryFilter] = useState('all')
  const importInputRef = useRef(null)
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

  const { data: authStatus } = useQuery({
    queryKey: ['auth', 'status'],
    queryFn: authApi.status,
  })
  const isAdmin = Boolean(authStatus?.is_admin)

  const { data: appSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
    enabled: isAdmin,
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

  const retryDownloadMutation = useMutation({
    mutationFn: (schedule) => downloadsApi.retry(schedule.download_id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'Download Queued',
        message: 'The download has been re-queued.',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Retry Failed',
        message: error.message,
        color: 'red',
      })
    },
  })

  const autoRetryMutation = useMutation({
    mutationFn: (enabled) => settingsApi.update({ auto_retry_failed_downloads: enabled }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
    },
    onError: (error) => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      notifications.show({
        title: 'Error',
        message: error.message,
        color: 'red',
      })
    },
  })

  const importMutation = useMutation({
    mutationFn: (doc) => schedulesApi.importDoc(doc),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      const skipped = result.skipped || []
      const details = skipped
        .slice(0, 3)
        .map((s) => `${s.title}: ${s.reason}`)
        .join('; ')
      notifications.show({
        title: 'Import Complete',
        message: skipped.length
          ? `Created ${result.created}, skipped ${skipped.length} (${details}${skipped.length > 3 ? '; ...' : ''}).`
          : `Created ${result.created} schedule${result.created === 1 ? '' : 's'}.`,
        color: skipped.length ? 'yellow' : 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Import Failed',
        message: error.message,
        color: 'red',
      })
    },
  })

  const handleExport = async () => {
    try {
      const doc = await schedulesApi.exportAll()
      const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `mustarrd-schedules-${dayjs().format('YYYY-MM-DD')}.json`
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
      notifications.show({
        title: 'Schedules Exported',
        message: `Exported ${doc.schedules.length} schedule${doc.schedules.length === 1 ? '' : 's'}.`,
        color: 'green',
      })
    } catch (error) {
      notifications.show({
        title: 'Export Failed',
        message: error.message,
        color: 'red',
      })
    }
  }

  const handleImportFile = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    let doc
    try {
      doc = JSON.parse(await file.text())
    } catch {
      notifications.show({
        title: 'Import Failed',
        message: 'The selected file is not a valid JSON schedule export.',
        color: 'red',
      })
      return
    }
    importMutation.mutate(doc)
  }

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

  const activeSchedules = (localItems?.filter((s) =>
    ['scheduled', 'queued', 'downloading', 'processing', 'paused_low_space'].includes(s.status)
  ) || []).sort((a, b) => new Date(a.program_start || 0) - new Date(b.program_start || 0))

  const historySchedules = localItems?.filter((s) =>
    ['completed', 'failed', 'cancelled'].includes(s.status)
  ) || []
  const filteredHistorySchedules = historyFilter === 'all'
    ? historySchedules
    : historySchedules.filter((s) => s.status === historyFilter)
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
      <Group justify="space-between" align="center" wrap="wrap">
        <Title order={2}>Scheduled Recordings</Title>
        <Group gap="sm">
          {isAdmin && (
            <Tooltip
              label="Automatically retry failed downloads while the program is still inside the channel's catchup window"
              multiline
              w={280}
            >
              <Switch
                size="sm"
                label="Auto-retry failed downloads"
                checked={Boolean(appSettings?.auto_retry_failed_downloads)}
                onChange={(event) => autoRetryMutation.mutate(event.currentTarget.checked)}
                disabled={autoRetryMutation.isPending || !appSettings}
              />
            </Tooltip>
          )}
          <Group gap="xs" wrap="nowrap">
            <Button
              size="xs"
              variant="default"
              leftSection={<IconFileExport size={14} />}
              onClick={handleExport}
            >
              Export
            </Button>
            <Button
              size="xs"
              variant="default"
              leftSection={<IconFileImport size={14} />}
              onClick={() => importInputRef.current?.click()}
              loading={importMutation.isPending}
            >
              Import
            </Button>
          </Group>
          <input
            ref={importInputRef}
            type="file"
            accept=".json,application/json"
            style={{ display: 'none' }}
            onChange={handleImportFile}
          />
        </Group>
      </Group>

      <Tabs
        value={['upcoming', 'history'].includes(searchParams.get('tab')) ? searchParams.get('tab') : 'upcoming'}
        onChange={(val) => setSearchParams({ tab: val }, { replace: true })}
      >
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
                  onRetryDownload={(s) => retryDownloadMutation.mutate(s)}
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
              <Select
                size="xs"
                w={160}
                value={historyFilter}
                onChange={(v) => setHistoryFilter(v || 'all')}
                leftSection={<IconFilter size={14} />}
                allowDeselect={false}
                data={[
                  { value: 'all', label: 'All statuses' },
                  { value: 'completed', label: 'Completed' },
                  { value: 'failed', label: 'Failed' },
                  { value: 'cancelled', label: 'Cancelled' },
                ]}
              />
              {filteredHistorySchedules.length === 0 ? (
                <Text c="dimmed" ta="center" py="lg" size="sm">
                  No {historyFilter} scheduled recordings.
                </Text>
              ) : filteredHistorySchedules.map((schedule) => (
                <ScheduleCard
                  key={schedule.id}
                  schedule={schedule}
                  guideOffsetHours={accountGuideOffsets[Number(schedule.account_id)] || 0}
                  isDesktop={isDesktop}
                  onCancel={(s) => cancelMutation.mutate(s)}
                  onDelete={(s) => deleteMutation.mutate(s)}
                  onOpenFileLocation={handleOpenFileLocation}
                  onPlayFile={handlePlayFile}
                  onRetryDownload={(s) => retryDownloadMutation.mutate(s)}
                />
              ))}
            </Stack>
          )}
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}
