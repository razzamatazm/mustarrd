import { useState, useEffect } from 'react'
import {
  Title,
  Card,
  Group,
  Text,
  Stack,
  Badge,
  ActionIcon,
  Modal,
  Tabs,
  Loader,
  Alert,
  Button,
  Collapse,
  SegmentedControl,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useNavigate, Link, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
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
  IconAlertTriangle,
} from '@tabler/icons-react'
import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'

import { accountsApi, authApi, downloadsApi, createDownloadWebSocket } from '../api'
import HistoryRow from '../components/HistoryRow'
import { formatChannelDateTime, formatAirDateTime, getGuideOffsetHours } from '../utils/channelTime'
import {
  PLAYABLE_DOWNLOAD_STATUSES,
  RETRYABLE_DOWNLOAD_STATUSES,
  TERMINAL_DOWNLOAD_STATUSES,
} from '../constants/downloadStatus'
import classes from './Downloads.module.css'

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

// "54 min of 60 min" for an interrupted recording, so the user can see how
// much of the program was actually captured. Null when we have no probed
// duration to compare against.
function formatRecordedVsExpected(download) {
  const recorded = download?.recorded_duration_seconds
  const expected = download?.duration_minutes
  if (!recorded || recorded <= 0 || !expected || expected <= 0) return null
  return `${Math.round(recorded / 60)} min of ${expected} min`
}

// An interrupted recording carries two independent messages: why the capture
// stopped, and any "Completed with warnings" integrity note. Show both, and
// nothing at all when there is neither.
function renderRowMessages(download) {
  const messages = [download.interruption_reason, download.error_message].filter(Boolean)
  if (messages.length === 0) return null
  return messages.map((msg, i) => <div key={i}>{renderErrorMessage(msg)}</div>)
}

function getStatusBadge(status) {
  const statusConfig = {
    pending: { color: 'gray', icon: IconClock, label: 'Pending' },
    downloading: { color: 'yellow', icon: IconDownload, label: 'Downloading' },
    processing: { color: 'teal', icon: IconSettings, label: 'Processing' },
    completed: { color: 'green', icon: IconCheck, label: 'Completed' },
    failed: { color: 'red', icon: IconX, label: 'Failed' },
    cancelled: { color: 'orange', icon: IconX, label: 'Cancelled' },
    interrupted: { color: 'grape', icon: IconAlertTriangle, label: 'Interrupted' },
  }

  const config = statusConfig[status] || statusConfig.pending
  const Icon = config.icon

  return (
    <Badge color={config.color} variant="light" leftSection={<Icon size={12} />}>
      {config.label}
    </Badge>
  )
}

function PipelineStage({ name, percent, indeterminate, done, idle }) {
  const display = done ? '✓' : idle ? '—' : indeterminate ? '…' : `${Math.round(percent)}%`
  const width = done ? 100 : Math.min(100, Math.max(0, percent || 0))
  const stageClasses = [
    classes.stage,
    done ? classes.stageDone : '',
    idle ? classes.stageIdle : '',
  ].filter(Boolean).join(' ')

  return (
    <div className={stageClasses}>
      <div className={classes.stageLabel}>
        <span className={classes.stageName}>{name}</span>
        <span className={classes.stagePercent}>{display}</span>
      </div>
      <div className={classes.bar}>
        {indeterminate && !done ? (
          <div className={classes.fillIndet} />
        ) : (
          <div className={classes.fill} style={{ width: `${width}%` }} />
        )}
      </div>
    </div>
  )
}

function ActiveDownloadCard({ download, isAdmin, onCancel, guideOffsetHours = 0 }) {
  const [showLogDetails, setShowLogDetails] = useState(false)

  const status = download.status
  const downloadProgress = typeof download.download_progress === 'number'
    ? download.download_progress
    : (status === 'processing' ? 100 : (download.progress ?? 0))
  const comskipProgress = typeof download.comskip_progress === 'number' ? download.comskip_progress : null
  const transcodeProgress = typeof download.transcode_progress === 'number' ? download.transcode_progress : null
  const comskipIndeterminate = Boolean(download.comskip_indeterminate)
  const transcodeIndeterminate = Boolean(download.transcode_indeterminate)
  const hasComskipStage = comskipProgress !== null || comskipIndeterminate
  const hasTranscodeStage = transcodeProgress !== null || transcodeIndeterminate

  const airedWindow = `${formatAirDateTime(download, 'start', guideOffsetHours) || 'Unknown'} - ${formatChannelDateTime(download, 'end', guideOffsetHours, 'h:mm A') || 'Unknown'} (${formatDuration(download.duration_minutes)})`
  const requestedBy = download.requested_by?.display_name ||
    download.requested_by?.username ||
    (download.requested_by_user_id ? `User #${download.requested_by_user_id}` : 'Unknown')

  return (
    <div className={classes.card}>
      <div className={classes.cardHead}>
        <div className={classes.cardTitleWrap}>
          <div className={classes.cardTitle}>{download.program_title}</div>
          <div className={classes.cardSub}>
            {download.channel_name} · Aired {airedWindow}
            {isAdmin && <> · Requested by {requestedBy}</>}
          </div>
        </div>
        {getStatusBadge(status)}
        <ActionIcon
          variant="subtle"
          color="gray"
          size={30}
          radius={7}
          aria-label="Cancel download"
          title="Cancel download"
          onClick={() => onCancel(download)}
        >
          <IconX size={16} />
        </ActionIcon>
      </div>

      <div className={classes.pipe}>
        <PipelineStage
          name="Download"
          percent={downloadProgress}
          indeterminate={Boolean(download.indeterminate) && status === 'downloading'}
          done={status === 'processing' || downloadProgress >= 100}
          idle={status === 'pending'}
        />
        {hasComskipStage && (
          <PipelineStage
            name="Commercials"
            percent={comskipProgress ?? 0}
            indeterminate={comskipIndeterminate}
            done={comskipProgress !== null && comskipProgress >= 100 && !comskipIndeterminate}
            idle={false}
          />
        )}
        {hasTranscodeStage && (
          <PipelineStage
            name="Re-encode"
            percent={transcodeProgress ?? 0}
            indeterminate={transcodeIndeterminate}
            done={transcodeProgress !== null && transcodeProgress >= 100 && !transcodeIndeterminate}
            idle={false}
          />
        )}
      </div>

      <div className={classes.meta}>
        {status === 'downloading' && (
          <span>{formatBytes(download.downloaded_bytes)} / {formatBytes(download.file_size)}</span>
        )}
        {status === 'processing' && <span>{download.message || 'Processing...'}</span>}
        {status === 'pending' && <span>Waiting in queue...</span>}
      </div>

      {download.logs?.length > 0 && (
        <Stack gap={6} mt={8}>
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
    </div>
  )
}

export default function Downloads() {
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
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

        if (TERMINAL_DOWNLOAD_STATUSES.includes(data.status)) {
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
    TERMINAL_DOWNLOAD_STATUSES.includes(d.status)
  ) || []

  const filteredHistoryDownloads = historyFilter === 'all'
    ? historyDownloads
    : historyDownloads.filter((d) => d.status === historyFilter)
  const accountGuideOffsets = Object.fromEntries(
    (accounts || []).map((account) => [Number(account.id), getGuideOffsetHours(account.guide_offset_hours)])
  )

  const renderHistoryActions = (download) => {
    const canRetry = RETRYABLE_DOWNLOAD_STATUSES.includes(download.status)
    const retryLabel = download.status === 'cancelled' ? 'Download again' : 'Retry'
    const downloadHref = `/api/downloads/${download.id}/file?action=download`
    const playHref = `/downloads/${download.id}/play`

    return (
      <>
        {PLAYABLE_DOWNLOAD_STATUSES.includes(download.status) && (
          isDesktop ? (
            <>
              <ActionIcon
                variant="subtle"
                color="gray"
                size={30}
                radius={7}
                aria-label="Play"
                title="Play"
                onClick={() => handlePlayFile(download)}
              >
                <IconPlayerPlay size={15} />
              </ActionIcon>
              <ActionIcon
                variant="subtle"
                color="gray"
                size={30}
                radius={7}
                aria-label="Open file location"
                title="Open file location"
                onClick={() => handleOpenFileLocation(download)}
              >
                <IconFolderOpen size={15} />
              </ActionIcon>
            </>
          ) : (
            <>
              <ActionIcon
                component="a"
                href={playHref}
                variant="subtle"
                color="gray"
                size={30}
                radius={7}
                aria-label="Play"
                title="Play"
              >
                <IconPlayerPlay size={15} />
              </ActionIcon>
              <ActionIcon
                component="a"
                href={downloadHref}
                variant="subtle"
                color="gray"
                size={30}
                radius={7}
                aria-label="Download file"
                title="Download file"
              >
                <IconDownload size={15} />
              </ActionIcon>
            </>
          )
        )}
        {canRetry && (
          <ActionIcon
            variant="subtle"
            color="gray"
            size={30}
            radius={7}
            aria-label={retryLabel}
            title={retryLabel}
            onClick={() => retryMutation.mutate(download)}
          >
            <IconRefresh size={15} />
          </ActionIcon>
        )}
        <ActionIcon
          variant="subtle"
          color="red"
          size={30}
          radius={7}
          aria-label="Remove from history"
          title="Remove from history"
          onClick={() => deleteMutation.mutate(download)}
        >
          <IconTrash size={15} />
        </ActionIcon>
      </>
    )
  }

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
    <Stack className={classes.page}>
      <Group justify="space-between" align="center">
        <Title order={2}>Downloads</Title>
        {diskSpace && (
          <Badge
            color={diskSpace.available === false ? 'orange' : diskSpace.is_low ? 'red' : 'gray'}
            variant={diskSpace.available === false || diskSpace.is_low ? 'filled' : 'outline'}
            leftSection={<IconDatabase size={12} />}
            size="lg"
          >
            {diskSpace.available === false
              ? 'Recording drive not found'
              : `${diskSpace.disk_free_gb} GB free${diskSpace.is_low ? ': Low disk space' : ''}`}
          </Badge>
        )}
      </Group>

      <Tabs
        value={['active', 'history'].includes(searchParams.get('tab')) ? searchParams.get('tab') : 'active'}
        onChange={(val) => setSearchParams({ tab: val }, { replace: true })}
      >
        <Tabs.List>
          <Tabs.Tab value="active" leftSection={<IconDownload size={16} />}>
            Active {activeDownloads.length > 0 && `(${activeDownloads.length})`}
          </Tabs.Tab>
          <Tabs.Tab value="history" leftSection={<IconClock size={16} />}>
            History {historyDownloads.length > 0 && `(${historyDownloads.length})`}
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="active" pt="md">
          {activeDownloads.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconDownload size={40} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No active downloads.
                </Text>
                <Text c="dimmed" ta="center" size="sm" maw={380}>
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
                <ActiveDownloadCard
                  key={download.id}
                  download={download}
                  isAdmin={Boolean(authStatus?.is_admin)}
                  onCancel={(d) => cancelMutation.mutate(d)}
                  guideOffsetHours={accountGuideOffsets[Number(download.account_id)] || 0}
                />
              ))}
            </Stack>
          )}
          <Text size="xs" c="dimmed" mt="sm">
            Recordings scheduled for later live on the{' '}
            <Link to="/scheduled" style={{ color: 'var(--mantine-color-yellow-7)' }}>Scheduled</Link> page.
          </Text>
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="md">
          {historyDownloads.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconClock size={40} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No download history yet.
                </Text>
                <Text c="dimmed" ta="center" size="sm" maw={380}>
                  Completed, failed, and cancelled downloads will appear here.
                </Text>
              </Stack>
            </Card>
          ) : (
            <Stack gap="xs">
              <Group justify="space-between">
                <SegmentedControl
                  size="xs"
                  value={historyFilter}
                  onChange={(v) => setHistoryFilter(v || 'all')}
                  data={[
                    { value: 'all', label: 'All' },
                    { value: 'completed', label: 'Completed' },
                    { value: 'failed', label: 'Failed' },
                    { value: 'cancelled', label: 'Cancelled' },
                    { value: 'interrupted', label: 'Interrupted' },
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
                <div>
                  {filteredHistoryDownloads.map((download) => {
                    const guideOffset = accountGuideOffsets[Number(download.account_id)] || 0
                    const aired = formatAirDateTime(download, 'start', guideOffset)
                    const subtitleParts = [download.channel_name, aired]
                    if (
                      PLAYABLE_DOWNLOAD_STATUSES.includes(download.status) &&
                      download.output_path
                    ) {
                      subtitleParts.push(getFileName(download.output_path))
                    }
                    if (download.status === 'interrupted') {
                      const captured = formatRecordedVsExpected(download)
                      subtitleParts.unshift(
                        captured ? `Interrupted - ${captured}` : 'Interrupted'
                      )
                    }
                    return (
                      <HistoryRow
                        key={download.id}
                        status={download.status}
                        title={download.program_title}
                        subtitle={subtitleParts.filter(Boolean).join(' · ')}
                        error={renderRowMessages(download)}
                        size={PLAYABLE_DOWNLOAD_STATUSES.includes(download.status) && download.file_size > 0 ? formatBytes(download.file_size) : null}
                        actions={renderHistoryActions(download)}
                      />
                    )
                  })}
                </div>
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
