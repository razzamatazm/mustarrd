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
  Modal,
  TextInput,
  Select,
  MultiSelect,
  NumberInput,
  SimpleGrid,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useNavigate, Link, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
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
  IconRefresh,
  IconFileExport,
  IconFileImport,
  IconRepeat,
  IconPencil,
} from '@tabler/icons-react'
import dayjs from 'dayjs'
import duration from 'dayjs/plugin/duration'

import { accountsApi, authApi, downloadsApi, recordingRulesApi, schedulesApi, settingsApi } from '../api'
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

const matchModeLabels = {
  exact: 'Exact title match',
  contains: 'Title contains',
  regex: 'Regular expression',
}

const weekdayOptions = [
  { value: '0', label: 'Mon' },
  { value: '1', label: 'Tue' },
  { value: '2', label: 'Wed' },
  { value: '3', label: 'Thu' },
  { value: '4', label: 'Fri' },
  { value: '5', label: 'Sat' },
  { value: '6', label: 'Sun' },
]

function RuleEditorModal({ opened, onClose, rule, accountName, onSave, saving }) {
  const [titleMatch, setTitleMatch] = useState('')
  const [matchMode, setMatchMode] = useState('exact')
  const [enabled, setEnabled] = useState(true)
  const [daysOfWeek, setDaysOfWeek] = useState([])
  const [deleteAfterDays, setDeleteAfterDays] = useState('')
  const [prePadding, setPrePadding] = useState(0)
  const [postPadding, setPostPadding] = useState(0)
  const [patternError, setPatternError] = useState('')

  useEffect(() => {
    if (!opened || !rule) return
    setTitleMatch(rule.title_match || '')
    setMatchMode(rule.match_mode || 'exact')
    setEnabled(Boolean(rule.enabled))
    setDaysOfWeek((rule.days_of_week || []).map(String))
    setDeleteAfterDays(rule.delete_after_days ?? '')
    setPrePadding(rule.pre_padding_minutes || 0)
    setPostPadding(rule.post_padding_minutes || 0)
    setPatternError('')
  }, [opened, rule])

  const handleSave = async () => {
    const trimmedTitle = titleMatch.trim()
    if (!trimmedTitle) {
      setPatternError('A title or pattern is required')
      return
    }
    if (matchMode === 'regex') {
      try {
        new RegExp(trimmedTitle, 'i')
      } catch (error) {
        setPatternError(error.message)
        return
      }
    }
    setPatternError('')
    await onSave(rule, {
      title_match: trimmedTitle,
      match_mode: matchMode,
      enabled,
      days_of_week: daysOfWeek.map(Number),
      delete_after_days: typeof deleteAfterDays === 'number' ? deleteAfterDays : null,
      pre_padding_minutes: typeof prePadding === 'number' ? prePadding : 0,
      post_padding_minutes: typeof postPadding === 'number' ? postPadding : 0,
    })
    onClose()
  }

  if (!rule) return null

  return (
    <Modal opened={opened} onClose={onClose} title="Edit recurring recording" size="lg">
      <Stack>
        <Text size="sm" c="dimmed">
          {rule.channel_name}{accountName ? ` · ${accountName}` : ''}
        </Text>
        <Switch
          label="Rule enabled"
          checked={enabled}
          onChange={(event) => setEnabled(event.currentTarget.checked)}
        />
        <Select
          label="Title matching"
          data={[
            { value: 'exact', label: 'Exact title (normalized)' },
            { value: 'contains', label: 'Title contains' },
            { value: 'regex', label: 'Regular expression' },
          ]}
          value={matchMode}
          onChange={(value) => {
            setMatchMode(value || 'exact')
            setPatternError('')
          }}
          allowDeselect={false}
        />
        <TextInput
          label={matchMode === 'regex' ? 'Title regular expression' : 'Title match'}
          description={matchMode === 'regex'
            ? 'Case-insensitive regex search, for example ^News at (6|9)$'
            : 'Matching is case-insensitive and repeated whitespace is ignored'}
          value={titleMatch}
          onChange={(event) => {
            setTitleMatch(event.currentTarget.value)
            setPatternError('')
          }}
          error={patternError}
        />
        <MultiSelect
          label="Days of week"
          description="Leave empty to match every day"
          data={weekdayOptions}
          value={daysOfWeek}
          onChange={setDaysOfWeek}
          clearable
        />
        <SimpleGrid cols={{ base: 1, sm: 2 }}>
          <NumberInput
            label="Minutes before start"
            min={0}
            max={120}
            value={prePadding}
            onChange={setPrePadding}
          />
          <NumberInput
            label="Minutes after end"
            min={0}
            max={120}
            value={postPadding}
            onChange={setPostPadding}
          />
        </SimpleGrid>
        <NumberInput
          label="Delete completed recordings after"
          description="Optional number of days; leave blank to keep forever. Older recordings are purged after the next successful EPG refresh."
          suffix=" days"
          min={1}
          max={3650}
          allowDecimal={false}
          value={deleteAfterDays}
          onChange={setDeleteAfterDays}
        />
        <Alert color="blue" variant="light">
          Saving immediately checks the current future guide. Recordings already scheduled by this rule are not cancelled or changed.
        </Alert>
        <Group justify="flex-end">
          <Button variant="subtle" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button onClick={handleSave} loading={saving}>Save rule</Button>
        </Group>
      </Stack>
    </Modal>
  )
}

function RuleRow({ rule, accountName, onToggle, onUpdate, onDelete, updating }) {
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [editing, setEditing] = useState(false)
  const retention = rule.delete_after_days
    ? `Deletes recordings after ${rule.delete_after_days} days`
    : 'Keeps recordings forever'
  const matchLabel = matchModeLabels[rule.match_mode || 'exact'] || matchModeLabels.exact
  const daysLabel = rule.days_of_week?.length
    ? rule.days_of_week.map((day) => weekdayOptions.find((option) => option.value === String(day))?.label).filter(Boolean).join(', ')
    : 'Every day'

  return (
    <div className={classes.row}>
      <span className={classes.chip}>{channelInitials(rule.channel_name)}</span>
      <div className={classes.main}>
        <div className={classes.title}>{rule.title_match}</div>
        <div className={classes.sub}>
          {[rule.channel_name, accountName, matchLabel, daysLabel, retention].filter(Boolean).join(' · ')}
        </div>
      </div>
      <div className={classes.side}>
        {confirmingDelete ? (
          <>
            <Text size="sm" c="dimmed">Delete this rule?</Text>
            <Button
              size="xs"
              color="red"
              onClick={() => {
                onDelete(rule)
                setConfirmingDelete(false)
              }}
            >
              Yes, delete
            </Button>
            <Button size="xs" variant="subtle" onClick={() => setConfirmingDelete(false)}>
              Keep
            </Button>
          </>
        ) : (
          <>
            <Switch
              aria-label={`Enable ${rule.title_match}`}
              checked={Boolean(rule.enabled)}
              onChange={(event) => onToggle(rule, event.currentTarget.checked)}
            />
            <ActionIcon
              variant="subtle"
              color="gray"
              size={30}
              radius={7}
              aria-label={`Edit rule ${rule.title_match}`}
              title="Edit rule"
              onClick={() => setEditing(true)}
            >
              <IconPencil size={15} />
            </ActionIcon>
            <ActionIcon
              variant="subtle"
              color="red"
              size={30}
              radius={7}
              aria-label={`Delete rule ${rule.title_match}`}
              title="Delete rule"
              onClick={() => setConfirmingDelete(true)}
            >
              <IconTrash size={15} />
            </ActionIcon>
          </>
        )}
      </div>
      <RuleEditorModal
        opened={editing}
        onClose={() => setEditing(false)}
        rule={rule}
        accountName={accountName}
        onSave={onUpdate}
        saving={updating}
      />
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

  const {
    data: recordingRules = [],
    isLoading: rulesLoading,
    error: rulesError,
  } = useQuery({
    queryKey: ['recording-rules'],
    queryFn: recordingRulesApi.list,
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

  const updateRuleMutation = useMutation({
    mutationFn: ({ rule, changes }) => recordingRulesApi.update(rule.id, changes),
    onSuccess: (result, variables) => {
      queryClient.invalidateQueries({ queryKey: ['recording-rules'] })
      queryClient.invalidateQueries({ queryKey: ['schedules'] })
      if (Object.keys(variables.changes).length > 1) {
        notifications.show({
          title: 'Recording Rule Updated',
          message: result.scheduled_count
            ? `Matched ${result.scheduled_count} additional future airing${result.scheduled_count === 1 ? '' : 's'}`
            : 'The updated rule is active for future guide refreshes',
          color: 'green',
        })
      }
    },
    onError: (error) => {
      queryClient.invalidateQueries({ queryKey: ['recording-rules'] })
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })

  const deleteRuleMutation = useMutation({
    mutationFn: (rule) => recordingRulesApi.delete(rule.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['recording-rules'] })
      notifications.show({
        title: 'Recording Rule Deleted',
        message: 'Already-created scheduled recordings were kept',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
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
  const accountNames = Object.fromEntries(
    (accounts || []).map((account) => [Number(account.id), account.name])
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
        value={['upcoming', 'rules', 'history'].includes(searchParams.get('tab')) ? searchParams.get('tab') : 'upcoming'}
        onChange={(val) => setSearchParams({ tab: val }, { replace: true })}
      >
        <Tabs.List>
          <Tabs.Tab value="upcoming" leftSection={<IconCalendar size={16} />}>
            Upcoming {activeSchedules.length > 0 && `(${activeSchedules.length})`}
          </Tabs.Tab>
          <Tabs.Tab value="rules" leftSection={<IconRepeat size={16} />}>
            Recurring {recordingRules.length > 0 && `(${recordingRules.length})`}
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

        <Tabs.Panel value="rules" pt="md">
          {rulesLoading ? (
            <Stack align="center" py="xl"><Loader /></Stack>
          ) : rulesError ? (
            <Alert icon={<IconAlertCircle size={16} />} title="Error" color="red">
              Failed to load recurring rules: {rulesError.message}
            </Alert>
          ) : recordingRules.length === 0 ? (
            <Card shadow="sm" padding="xl" radius="md" withBorder>
              <Stack align="center" gap="md">
                <IconRepeat size={40} opacity={0.3} />
                <Text c="dimmed" ta="center">
                  No recurring recording rules yet.
                </Text>
                <Text size="sm" c="dimmed" ta="center" maw={420}>
                  Open a future programme in Browse and choose “Record every airing”.
                </Text>
                <Button onClick={() => navigate('/browse')}>Go to Browse</Button>
              </Stack>
            </Card>
          ) : (
            <div>
              {recordingRules.map((rule) => (
                <RuleRow
                  key={rule.id}
                  rule={rule}
                  accountName={accountNames[Number(rule.account_id)]}
                  onToggle={(item, enabled) => updateRuleMutation.mutate({ rule: item, changes: { enabled } })}
                  onUpdate={(item, changes) => updateRuleMutation.mutateAsync({ rule: item, changes })}
                  onDelete={(item) => deleteRuleMutation.mutate(item)}
                  updating={updateRuleMutation.isPending}
                />
              ))}
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
