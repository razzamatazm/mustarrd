import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Title,
  Card,
  Group,
  Text,
  Stack,
  Badge,
  ActionIcon,
  Tabs,
  Loader,
  Alert,
  Button,
  SegmentedControl,
  Switch,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useNavigate, Link, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  IconTrash,
  IconAlertCircle,
  IconAlertTriangle,
  IconCheck,
  IconX,
  IconClock,
  IconCalendar,
  IconDownload,
  IconSettings,
  IconPlayerPlay,
  IconFolderOpen,
  IconRefresh,
  IconFileExport,
  IconFileImport,
} from '@tabler/icons-react'
import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'

import { accountsApi, authApi, downloadsApi, schedulesApi, settingsApi } from '../api'
import HistoryRow from '../components/HistoryRow'
import { formatChannelDateTime, formatAirDateTime, getGuideOffsetHours, getChannelDisplayTime, getNowUtc } from '../utils/channelTime'
import classes from './Scheduled.module.css'

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

function formatTimeUntil(displayTime, guideOffsetHours = 0) {
  if (!displayTime) return null
  const nowDisplay = getNowUtc().add(getGuideOffsetHours(guideOffsetHours), 'hour')
  const diffMinutes = displayTime.diff(nowDisplay, 'minute')
  if (diffMinutes <= 0) return null
  const hours = Math.floor(diffMinutes / 60)
  const mins = diffMinutes % 60
  if (hours >= 48) return `in ${Math.floor(hours / 24)}d`
  if (hours >= 24) {
    const remHours = hours % 24
    return remHours > 0 ? `in 1d ${remHours}h` : 'in 1d'
  }
  if (hours > 0) return mins > 0 ? `in ${hours}h ${mins}m` : `in ${hours}h`
  return `in ${mins}m`
}

function channelInitials(name) {
  const words = (name || '').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '?'
  return words
    .slice(0, 2)
    .map((word) => word[0])
    .join('')
    .toUpperCase()
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
    interrupted: { color: 'grape', icon: IconAlertTriangle, label: 'Interrupted' },
    paused_low_space: { color: 'yellow', icon: IconAlertCircle, label: 'Paused — low space' },
  }

  const config = statusConfig[status] || statusConfig.scheduled
  const Icon = config.icon

  return (
    <Badge color={config.color} variant="light" leftSection={<Icon size={12} />}>
      {config.label}
    </Badge>
  )
}

function AgendaRow({ schedule, guideOffsetHours = 0, onCancel, onRetryDownload }) {
  const [confirmingCancel, setConfirmingCancel] = useState(false)

  const startTime = getChannelDisplayTime(schedule, 'start', guideOffsetHours)
  const endTime = getChannelDisplayTime(schedule, 'end', guideOffsetHours)
  const availableAt = formatAirDateTime(schedule, 'available', guideOffsetHours)
  const downloadDisplayTime = getChannelDisplayTime(schedule, 'available', guideOffsetHours)
  const timeUntil = formatTimeUntil(downloadDisplayTime, guideOffsetHours)
  const totalDuration = (schedule.duration_minutes || 0)
    + (schedule.pre_padding_minutes || 0)
    + (schedule.post_padding_minutes || 0)
  const paddingNote = totalDuration !== (schedule.duration_minutes || 0)
    ? `${formatDuration(totalDuration)} with padding`
    : null

  const subParts = [
    schedule.channel_name,
    formatDuration(schedule.duration_minutes || 0),
    paddingNote,
    `Download starts ${availableAt || 'Unknown'}${timeUntil ? ` (${timeUntil})` : ''}`,
  ].filter(Boolean)

  const downloadFailed = schedule.download_status === 'failed'

  return (
    <div className={classes.row}>
      <div className={classes.time}>
        <div className={classes.timeStart}>{startTime?.format('h:mm A') || 'Unknown'}</div>
        <div className={classes.timeEnd}>{endTime ? `– ${endTime.format('h:mm A')}` : ''}</div>
      </div>
      <span className={classes.chip}>{channelInitials(schedule.channel_name)}</span>
      <div className={classes.main}>
        <div className={classes.title}>{schedule.program_title}</div>
        <div className={classes.sub}>{subParts.join(' · ')}</div>
        {schedule.status_message && (
          <Text size="xs" c="yellow.5" mt={4}>
            {schedule.status_message}
          </Text>
        )}
        {!schedule.status_message && downloadFailed && schedule.download_error_message && (
          <Text size="xs" c="red.4" mt={4}>
            {renderErrorMessage(schedule.download_error_message)}
          </Text>
        )}
      </div>
      <div className={classes.side}>
        {confirmingCancel ? (
          <>
            <Text size="sm" c="dimmed">Cancel this recording?</Text>
            <Button
              size="xs"
              color="red"
              onClick={() => {
                onCancel(schedule)
                setConfirmingCancel(false)
              }}
            >
              Yes, cancel
            </Button>
            <Button size="xs" variant="subtle" onClick={() => setConfirmingCancel(false)}>
              Keep
            </Button>
          </>
        ) : (
          <>
            {schedule.status !== 'scheduled' && getStatusBadge(schedule.status)}
            {downloadFailed && schedule.download_id && (
              <ActionIcon
                variant="subtle"
                color="gray"
                size={30}
                radius={7}
                aria-label="Retry download"
                title="Retry download"
                onClick={() => onRetryDownload(schedule)}
              >
                <IconRefresh size={15} />
              </ActionIcon>
            )}
            <ActionIcon
              variant="subtle"
              color="gray"
              size={30}
              radius={7}
              aria-label="Cancel recording"
              title="Cancel recording"
              onClick={() => setConfirmingCancel(true)}
            >
              <IconX size={16} />
            </ActionIcon>
          </>
        )}
      </div>
    </div>
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
    ['completed', 'failed', 'cancelled', 'interrupted'].includes(s.status)
  ) || []
  const filteredHistorySchedules = historyFilter === 'all'
    ? historySchedules
    : historySchedules.filter((s) => s.status === historyFilter)
  const accountGuideOffsets = Object.fromEntries(
    (accounts || []).map((account) => [Number(account.id), getGuideOffsetHours(account.guide_offset_hours)])
  )

  // Group upcoming recordings by air date (in each channel's display time),
  // soonest day first; the sort above keeps rows ordered inside each day.
  const agendaDays = useMemo(() => {
    const order = []
    const map = {}
    activeSchedules.forEach((schedule) => {
      const guideOffset = accountGuideOffsets[Number(schedule.account_id)] || 0
      const day = getChannelDisplayTime(schedule, 'start', guideOffset)?.startOf('day')
      const key = day ? day.format('YYYY-MM-DD') : 'unknown'
      if (!map[key]) {
        map[key] = { key, date: day, guideOffset, items: [] }
        order.push(key)
      }
      map[key].items.push(schedule)
    })
    return order.map((key) => map[key])
  }, [activeSchedules, accountGuideOffsets])

  const renderHistoryActions = (schedule) => {
    const hasFile = schedule.download_status === 'completed' && schedule.download_id
    const downloadHref = schedule.download_id
      ? `/api/downloads/${schedule.download_id}/file?action=download`
      : null
    const playHref = schedule.download_id
      ? `/downloads/${schedule.download_id}/play`
      : null

    return (
      <>
        {hasFile && (
          isDesktop ? (
            <>
              <ActionIcon
                variant="subtle"
                color="gray"
                size={30}
                radius={7}
                aria-label="Play"
                title="Play"
                onClick={() => handlePlayFile(schedule)}
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
                onClick={() => handleOpenFileLocation(schedule)}
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
        {schedule.download_status === 'failed' && schedule.download_id && (
          <ActionIcon
            variant="subtle"
            color="gray"
            size={30}
            radius={7}
            aria-label="Retry"
            title="Retry"
            onClick={() => retryDownloadMutation.mutate(schedule)}
          >
            <IconRefresh size={15} />
          </ActionIcon>
        )}
        <ActionIcon
          variant="subtle"
          color="red"
          size={30}
          radius={7}
          aria-label="Delete"
          title="Delete"
          onClick={() => deleteMutation.mutate(schedule)}
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
        Failed to load schedules: {error.message}
      </Alert>
    )
  }

  return (
    <Stack className={classes.page}>
      <Group justify="space-between" align="center" wrap="wrap">
        <Title order={2}>Scheduled</Title>
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
                <IconCalendar size={40} opacity={0.3} />
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
            <div>
              {agendaDays.map(({ key, date, guideOffset, items }) => {
                const displayNow = getNowUtc().add(getGuideOffsetHours(guideOffset), 'hour')
                const isToday = Boolean(date?.isSame(displayNow, 'day'))
                return (
                  <div className={classes.day} key={key}>
                    <div className={classes.dayHead}>
                      {isToday ? (
                        <span className={classes.dayToday}>TONIGHT</span>
                      ) : (
                        <span className={classes.dayDate}>
                          {date ? date.format('ddd, MMM D') : 'Unknown date'}
                        </span>
                      )}
                      <span className={classes.dayRule} />
                    </div>
                    {items.map((schedule) => (
                      <AgendaRow
                        key={schedule.id}
                        schedule={schedule}
                        guideOffsetHours={accountGuideOffsets[Number(schedule.account_id)] || 0}
                        onCancel={(s) => cancelMutation.mutate(s)}
                        onRetryDownload={(s) => retryDownloadMutation.mutate(s)}
                      />
                    ))}
                  </div>
                )
              })}
            </div>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="md">
          {historySchedules.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconClock size={40} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No schedule history yet.
                </Text>
                <Text c="dimmed" ta="center" size="sm" maw={380}>
                  Completed, failed, and cancelled scheduled recordings will appear here.
                </Text>
              </Stack>
            </Card>
          ) : (
            <Stack gap="xs">
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
                style={{ alignSelf: 'flex-start' }}
              />
              {filteredHistorySchedules.length === 0 ? (
                <Text c="dimmed" ta="center" py="lg" size="sm">
                  No {historyFilter} scheduled recordings.
                </Text>
              ) : (
                <div>
                  {filteredHistorySchedules.map((schedule) => {
                    const guideOffset = accountGuideOffsets[Number(schedule.account_id)] || 0
                    const aired = formatAirDateTime(schedule, 'start', guideOffset)
                    const errorContent = schedule.status_message
                      || (schedule.download_status === 'failed' && schedule.download_error_message
                        ? renderErrorMessage(schedule.download_error_message)
                        : null)
                    return (
                      <HistoryRow
                        key={schedule.id}
                        status={schedule.status}
                        title={schedule.program_title}
                        subtitle={[schedule.channel_name, aired].filter(Boolean).join(' · ')}
                        error={errorContent}
                        actions={renderHistoryActions(schedule)}
                      />
                    )
                  })}
                </div>
              )}
            </Stack>
          )}
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}
