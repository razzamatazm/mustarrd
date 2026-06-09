import { useState, useEffect } from 'react'
import {
  Title,
  Card,
  Group,
  Text,
  Stack,
  Badge,
  ActionIcon,
  Menu,
  Modal,
  Tabs,
  Loader,
  Alert,
  Tooltip,
  Button,
  Collapse,
  Select,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useNavigate, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  IconDotsVertical,
  IconPlayerStop,
  IconRefresh,
  IconTrash,
  IconAlertCircle,
  IconCheck,
  IconX,
  IconClock,
  IconDownload,
  IconSettings,
  IconPlayerPlay,
  IconFolderOpen,
  IconChevronDown,
  IconChevronUp,
  IconDatabase,
  IconCalendar,
  IconFilter,
} from '@tabler/icons-react'
import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'

import { accountsApi, authApi, downloadsApi, createDownloadWebSocket } from '../api'
import ProgressBar from '../components/ProgressBar'
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

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`
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

function getFileName(filePath) {
  if (!filePath || typeof filePath !== 'string') return 'file'
  const parts = filePath.split(/[\\/]/)
  return parts[parts.length - 1] || 'file'
}

function getStatusBadge(status) {
  const statusConfig = {
    pending: { color: 'gray', icon: IconClock, label: 'Pending' },
    downloading: { color: 'yellow', icon: IconDownload, label: 'Downloading' },
    processing: { color: 'teal', icon: IconSettings, label: 'Processing' },
    completed: { color: 'green', icon: IconCheck, label: 'Completed' },
    failed: { color: 'red', icon: IconX, label: 'Failed' },
    cancelled: { color: 'orange', icon: IconX, label: 'Cancelled' },
  }

  const config = statusConfig[status] || statusConfig.pending
  const Icon = config.icon

  return (
    <Badge color={config.color} variant="light" leftSection={<Icon size={12} />}>
      {config.label}
    </Badge>
  )
}

function getScheduledStatusBadge(status) {
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
    <Badge color={config.color} variant="light" size="sm" leftSection={<Icon size={12} />}>
      {config.label}
    </Badge>
  )
}

function DownloadCard({
  download,
  isAdmin,
  isDesktop,
  onCancel,
  onRetry,
  onDelete,
  onOpenFileLocation,
  onPlayFile,
  guideOffsetHours = 0,
}) {
  const [showLogDetails, setShowLogDetails] = useState(false)
  const isActive = ['pending', 'downloading', 'processing'].includes(download.status)
  const canRetry = ['failed', 'cancelled'].includes(download.status)
  const downloadProgress = typeof download.download_progress === 'number'
    ? download.download_progress
    : (download.status === 'processing' ? 100 : (download.progress ?? 0))
  const comskipProgress = typeof download.comskip_progress === 'number' ? download.comskip_progress : null
  const transcodeProgress = typeof download.transcode_progress === 'number' ? download.transcode_progress : null
  const comskipIndeterminate = Boolean(download.comskip_indeterminate)
  const transcodeIndeterminate = Boolean(download.transcode_indeterminate)
  const completedFileName = getFileName(download.output_path)
  const downloadHref = `/api/downloads/${download.id}/file?action=download`
  const playHref = `/downloads/${download.id}/play`

  const formatStagePercent = (value, indeterminate = false) => {
    if (indeterminate) return '...'
    if (typeof value !== 'number') return '—'
    return `${Math.round(value)}%`
  }

  return (
    <Card shadow="sm" padding="md" radius="md" withBorder>
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <Stack gap={2} style={{ flex: 1, overflow: 'hidden' }}>
            <Text fw={500}>
              {download.program_title}
            </Text>
            <Text size="sm" c="dimmed" truncate>
              {download.channel_name}
            </Text>
          </Stack>
          <Group gap="xs">
            {getStatusBadge(download.status)}
            <Menu shadow="md" width={150}>
              <Menu.Target>
                <ActionIcon variant="subtle">
                  <IconDotsVertical size={16} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                {isActive && (
                  <Menu.Item
                    leftSection={<IconPlayerStop size={14} />}
                    onClick={() => onCancel(download)}
                  >
                    Cancel
                  </Menu.Item>
                )}
                {canRetry && (
                  <Menu.Item
                    leftSection={<IconRefresh size={14} />}
                    onClick={() => onRetry(download)}
                  >
                    Retry
                  </Menu.Item>
                )}
                {!isActive && (
                  <Menu.Item
                    color="red"
                    leftSection={<IconTrash size={14} />}
                    onClick={() => onDelete(download)}
                  >
                    Delete
                  </Menu.Item>
                )}
              </Menu.Dropdown>
            </Menu>
          </Group>
        </Group>

        <Group gap="xs" wrap="nowrap">
          <Text size="xs" c="dimmed">
            {formatChannelDateTime(download, 'start', guideOffsetHours, 'MMM D, YYYY h:mm A') || 'Unknown'}
          </Text>
          <Text size="xs" c="dimmed">
            ({formatDuration(download.duration_minutes)})
          </Text>
        </Group>

        {isAdmin && (
          <Text size="xs" c="dimmed">
            Requested by:{' '}
            {download.requested_by?.display_name ||
              download.requested_by?.username ||
              (download.requested_by_user_id ? `User #${download.requested_by_user_id}` : 'Unknown')}
          </Text>
        )}

        {['downloading', 'processing'].includes(download.status) && (
          <Stack gap={6}>
            <Group justify="space-between">
              <Text size="xs" c="dimmed">Download</Text>
              <Text size="xs" c="dimmed">
                {formatStagePercent(downloadProgress, download.indeterminate && download.status === 'downloading')}
              </Text>
            </Group>
            <ProgressBar
              progress={downloadProgress}
              color="blue"
              indeterminate={download.indeterminate && download.status === 'downloading'}
            />
            {download.status === 'downloading' && (
              <Text size="xs" c="dimmed">
                {formatBytes(download.downloaded_bytes)} / {formatBytes(download.file_size)}
              </Text>
            )}

            {(comskipProgress !== null || comskipIndeterminate) && (
              <>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed">Commercial Detect</Text>
                  <Text size="xs" c="dimmed">
                    {formatStagePercent(comskipProgress, comskipIndeterminate)}
                  </Text>
                </Group>
                <ProgressBar
                  progress={comskipProgress ?? 0}
                  color="orange"
                  indeterminate={comskipIndeterminate}
                />
              </>
            )}

            {(transcodeProgress !== null || transcodeIndeterminate) && (
              <>
                <Group justify="space-between">
                  <Text size="xs" c="dimmed">Re-encode</Text>
                  <Text size="xs" c="dimmed">
                    {formatStagePercent(transcodeProgress, transcodeIndeterminate)}
                  </Text>
                </Group>
                <ProgressBar
                  progress={transcodeProgress ?? 0}
                  color="teal"
                  indeterminate={transcodeIndeterminate}
                />
              </>
            )}

            {download.status === 'processing' && (
              <Text size="xs" c="dimmed">
                {download.message || 'Processing...'}
              </Text>
            )}
          </Stack>
        )}

        {download.logs?.length > 0 && (
          <Stack gap={6}>
            <Group>
              <Button
                size="compact-xs"
                variant="subtle"
                leftSection={showLogDetails ? <IconChevronUp size={12} /> : <IconChevronDown size={12} />}
                onClick={() => setShowLogDetails((prev) => !prev)}
              >
                {showLogDetails ? 'Hide log details' : 'Show log details'}
              </Button>
            </Group>
            <Collapse in={showLogDetails}>
              <Card withBorder radius="sm" p="xs" bg="dark.8">
                <Stack gap={2}>
                  {download.logs.slice(-6).map((line, index) => (
                    <Text key={`${download.id}-log-${index}`} size="xs" c="dimmed" ff="monospace">
                      {line}
                    </Text>
                  ))}
                </Stack>
              </Card>
            </Collapse>
          </Stack>
        )}

        {download.status === 'completed' && (
          <Tooltip label={download.output_path}>
            <Text size="xs" c="dimmed" truncate>
              Saved to: {completedFileName}
            </Text>
          </Tooltip>
        )}
        {download.status === 'completed' && download.file_size > 0 && (
          <Text size="xs" c="dimmed">
            Download size: {formatBytes(download.file_size)}
          </Text>
        )}
        {download.status === 'completed' && (
          <Group gap="xs">
            {isDesktop ? (
              <>
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconFolderOpen size={14} />}
                  onClick={() => onOpenFileLocation(download)}
                >
                  Open File Location
                </Button>
                <Button
                  size="xs"
                  variant="default"
                  leftSection={<IconPlayerPlay size={14} />}
                  onClick={() => onPlayFile(download)}
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
        {download.status === 'completed' && download.error_message && (
          <Alert color="yellow" variant="light" p="xs">
            <Text size="xs">{renderErrorMessage(download.error_message)}</Text>
          </Alert>
        )}

        {download.status === 'failed' && download.error_message && (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs">{renderErrorMessage(download.error_message)}</Text>
          </Alert>
        )}
        {canRetry && (
          <Group gap="xs">
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} />}
              onClick={() => onRetry(download)}
            >
              Retry
            </Button>
          </Group>
        )}
      </Stack>
    </Card>
  )
}

export default function Downloads() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [localProgress, setLocalProgress] = useState({})
  const [localLogs, setLocalLogs] = useState({})
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false)
  const [historyFilter, setHistoryFilter] = useState('all')
  const desktopApi = typeof window !== 'undefined' ? window.mustarrdDesktop : null
  const isDesktop = Boolean(desktopApi?.openFileLocation && desktopApi?.playFile)

  const { data: downloads, isLoading, error } = useQuery({
    queryKey: ['downloads'],
    queryFn: downloadsApi.list,
    refetchInterval: 5000,
  })
  const { data: authStatus } = useQuery({
    queryKey: ['auth', 'status'],
    queryFn: authApi.status,
  })
  const { data: accounts } = useQuery({
    queryKey: ['accounts', 'public'],
    queryFn: accountsApi.publicList,
  })

  const { data: diskSpace } = useQuery({
    queryKey: ['downloads', 'disk-space'],
    queryFn: downloadsApi.diskSpace,
    refetchInterval: 30000,
  })

  const { data: upcomingRecordings } = useQuery({
    queryKey: ['downloads', 'upcoming'],
    queryFn: downloadsApi.upcoming,
    refetchInterval: 30000,
  })

  useEffect(() => {
    localStorage.setItem('mustarrd_downloads_visited', String(Date.now()))
    queryClient.invalidateQueries({ queryKey: ['downloads', 'failed-count'] })
  }, [queryClient])

  useEffect(() => {
    const ws = createDownloadWebSocket((data) => {
      if (data.type === 'progress') {
        setLocalProgress((prev) => ({
          ...prev,
          [data.download_id]: {
            ...(prev[data.download_id] || {}),
            progress: data.progress ?? prev[data.download_id]?.progress,
            status: data.status ?? prev[data.download_id]?.status,
            downloaded_bytes: data.downloaded_bytes ?? prev[data.download_id]?.downloaded_bytes,
            file_size: data.file_size ?? prev[data.download_id]?.file_size,
            message: data.message ?? prev[data.download_id]?.message,
            indeterminate: data.indeterminate ?? prev[data.download_id]?.indeterminate,
            download_progress: data.download_progress ?? prev[data.download_id]?.download_progress,
            comskip_progress: data.comskip_progress ?? prev[data.download_id]?.comskip_progress,
            transcode_progress: data.transcode_progress ?? prev[data.download_id]?.transcode_progress,
            comskip_indeterminate: data.comskip_indeterminate ?? prev[data.download_id]?.comskip_indeterminate,
            transcode_indeterminate: data.transcode_indeterminate ?? prev[data.download_id]?.transcode_indeterminate,
          },
        }))

        if (['completed', 'failed', 'cancelled'].includes(data.status)) {
          queryClient.invalidateQueries({ queryKey: ['downloads'] })
        }
      } else if (data.type === 'log') {
        setLocalLogs((prev) => {
          const existing = prev[data.download_id] || []
          const timestamp = dayjs().format('HH:mm:ss')
          const next = [...existing, `[${timestamp}] ${data.message}`].slice(-200)
          return { ...prev, [data.download_id]: next }
        })
      }
    })

    return () => ws.close()
  }, [queryClient])

  const cancelMutation = useMutation({
    mutationFn: (download) => downloadsApi.cancel(download.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'Download Cancelled',
        message: 'The download has been cancelled',
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

  const retryMutation = useMutation({
    mutationFn: (download) => downloadsApi.retry(download.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'Download Restarted',
        message: 'The download has been queued',
        color: 'yellow',
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
    mutationFn: (download) => downloadsApi.cancel(download.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'Download Removed',
        message: 'The download has been removed from history',
        color: 'green',
      })
    },
  })

  const clearFinishedMutation = useMutation({
    mutationFn: downloadsApi.clearFinished,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'History Cleared',
        message: data.deleted === 0
          ? 'Nothing to clear'
          : `Removed ${data.deleted} finished download${data.deleted === 1 ? '' : 's'}`,
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })

  const handleOpenFileLocation = async (download) => {
    if (!desktopApi?.openFileLocation) return
    try {
      const result = await desktopApi.openFileLocation(download.output_path)
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

  const handlePlayFile = async (download) => {
    if (!desktopApi?.playFile) return
    try {
      const result = await desktopApi.playFile(download.output_path)
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

  const enhancedDownloads = downloads?.map((d) => ({
    ...d,
    ...localProgress[d.id],
    logs: localLogs[d.id],
  }))

  const activeDownloads = enhancedDownloads?.filter((d) =>
    ['pending', 'downloading', 'processing'].includes(d.status)
  ) || []

  const historyDownloads = enhancedDownloads?.filter((d) =>
    ['completed', 'failed', 'cancelled'].includes(d.status)
  ) || []

  const filteredHistoryDownloads = historyFilter === 'all'
    ? historyDownloads
    : historyDownloads.filter((d) => d.status === historyFilter)
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
        Failed to load downloads: {error.message}
      </Alert>
    )
  }

  return (
    <Stack>
      <Group justify="space-between" align="center">
        <Title order={2}>Downloads</Title>
        {diskSpace && (
          <Badge
            color={diskSpace.available === false ? 'orange' : diskSpace.is_low ? 'red' : 'gray'}
            variant={diskSpace.available === false || diskSpace.is_low ? 'filled' : 'light'}
            leftSection={<IconDatabase size={12} />}
            size="lg"
          >
            {diskSpace.available === false
              ? 'Recording drive not found'
              : `${diskSpace.disk_free_gb} GB free${diskSpace.is_low ? ': Low disk space' : ''}`}
          </Badge>
        )}
      </Group>

      <Tabs defaultValue="active">
        <Tabs.List grow style={{ flexWrap: 'nowrap' }}>
          <Tabs.Tab value="active" leftSection={<IconDownload size={16} />}>
            Active {activeDownloads.length > 0 && `(${activeDownloads.length})`}
          </Tabs.Tab>
          <Tabs.Tab value="upcoming" leftSection={<IconCalendar size={16} />}>
            Upcoming {upcomingRecordings?.length > 0 && `(${upcomingRecordings.length})`}
          </Tabs.Tab>
          <Tabs.Tab value="history" leftSection={<IconClock size={16} />}>
            History {historyDownloads.length > 0 && `(${historyDownloads.length})`}
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="active" pt="md">
          {activeDownloads.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconDownload size={48} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No active downloads.
                </Text>
                <Text c="dimmed" ta="center" size="sm">
                  Find a show in Browse, then click Download on any program.
                </Text>
                <Button onClick={() => navigate('/browse')}>
                  Go to Browse
                </Button>
              </Stack>
            </Card>
          ) : (
            <Stack gap="md">
              {activeDownloads.map((download) => (
                <DownloadCard
                  key={download.id}
                  download={download}
                  isAdmin={Boolean(authStatus?.is_admin)}
                  isDesktop={isDesktop}
                  onCancel={(d) => cancelMutation.mutate(d)}
                  onRetry={(d) => retryMutation.mutate(d)}
                  onDelete={(d) => deleteMutation.mutate(d)}
                  onOpenFileLocation={handleOpenFileLocation}
                  onPlayFile={handlePlayFile}
                  guideOffsetHours={accountGuideOffsets[Number(download.account_id)] || 0}
                />
              ))}
            </Stack>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="upcoming" pt="md">
          {!upcomingRecordings || upcomingRecordings.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconCalendar size={48} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No upcoming recordings.
                </Text>
                <Text c="dimmed" ta="center" size="sm">
                  Schedule a show from the Browse page and it will appear here.
                </Text>
                <Button onClick={() => navigate('/browse')}>
                  Go to Browse
                </Button>
              </Stack>
            </Card>
          ) : (
            <Stack gap="sm">
              <Text size="xs" c="dimmed">
                To cancel or edit a recording, go to{' '}
                <Link to="/scheduled" style={{ color: 'var(--mantine-color-orange-5)' }}>Scheduled Recordings</Link>.
              </Text>
              {upcomingRecordings.map((rec) => {
                const guideOffset = accountGuideOffsets[Number(rec.account_id)] || 0
                const airStart = formatAirDateTime(rec, 'start', guideOffset)
                const airEnd = formatChannelDateTime(rec, 'end', guideOffset, 'h:mm A')
                const downloadAt = formatAirDateTime(rec, 'available', guideOffset)
                const totalDuration = (rec.duration_minutes || 0) + (rec.pre_padding_minutes || 0) + (rec.post_padding_minutes || 0)
                return (
                  <Card key={rec.id} shadow="sm" padding="md" radius="md" withBorder>
                    <Stack gap={2}>
                      <Group justify="space-between" wrap="nowrap" align="flex-start">
                        <Stack gap={2} style={{ flex: 1, overflow: 'hidden' }}>
                          <Text fw={500}>{rec.program_title}</Text>
                          <Text size="sm" c="dimmed" truncate>{rec.channel_name}</Text>
                        </Stack>
                        {getScheduledStatusBadge(rec.status)}
                      </Group>
                      <Text size="xs" c="dimmed">
                        Airs: {airStart || 'Unknown'} - {airEnd || 'Unknown'} ({formatDuration(rec.duration_minutes || 0)})
                      </Text>
                      <Text size="xs" c="dimmed">
                        Download starts: {downloadAt || 'Unknown'} ({formatDuration(totalDuration)})
                      </Text>
                    </Stack>
                  </Card>
                )
              })}
            </Stack>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="md">
          {historyDownloads.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconClock size={48} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No download history yet.
                </Text>
                <Text c="dimmed" ta="center" size="sm">
                  Completed, failed, and cancelled downloads will appear here.
                </Text>
              </Stack>
            </Card>
          ) : (
            <Stack gap="md">
              <Group justify="space-between">
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
                <Button
                  size="xs"
                  variant="subtle"
                  color="red"
                  leftSection={<IconTrash size={14} />}
                  onClick={() => setClearConfirmOpen(true)}
                  loading={clearFinishedMutation.isPending}
                >
                  Clear finished
                </Button>
              </Group>
              {filteredHistoryDownloads.length === 0 ? (
                <Text c="dimmed" ta="center" py="lg" size="sm">
                  No {historyFilter} downloads.
                </Text>
              ) : (
                filteredHistoryDownloads.map((download) => (
                  <DownloadCard
                    key={download.id}
                    download={download}
                    isAdmin={Boolean(authStatus?.is_admin)}
                    isDesktop={isDesktop}
                    onCancel={(d) => cancelMutation.mutate(d)}
                    onRetry={(d) => retryMutation.mutate(d)}
                    onDelete={(d) => deleteMutation.mutate(d)}
                    onOpenFileLocation={handleOpenFileLocation}
                    onPlayFile={handlePlayFile}
                    guideOffsetHours={accountGuideOffsets[Number(download.account_id)] || 0}
                  />
                ))
              )}
            </Stack>
          )}
        </Tabs.Panel>
      </Tabs>

      <Modal
        opened={clearConfirmOpen}
        onClose={() => setClearConfirmOpen(false)}
        title="Clear finished downloads?"
        size="sm"
      >
        <Text size="sm">
          This removes all completed and failed entries from history. Cancelled downloads stay so you can retry them.
        </Text>
        <Group justify="flex-end" mt="md">
          <Button variant="default" onClick={() => setClearConfirmOpen(false)}>
            Cancel
          </Button>
          <Button
            color="red"
            loading={clearFinishedMutation.isPending}
            onClick={() => {
              setClearConfirmOpen(false)
              clearFinishedMutation.mutate()
            }}
          >
            Clear finished
          </Button>
        </Group>
      </Modal>
    </Stack>
  )
}
