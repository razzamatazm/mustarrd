import { useMemo, useRef, useEffect } from 'react'
import { Stack, Text, Group, Badge, ScrollArea, Box, Divider, Tooltip } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { IconClock, IconDownload, IconCalendar } from '@tabler/icons-react'
import {
  getChannelDisplayTime,
  getGuideOffsetHours,
  getNowUtc,
  getProgramInstant,
} from '../utils/channelTime'

const previousDownloadStatusMeta = {
  pending: { label: 'Queued', color: 'yellow' },
  downloading: { label: 'Downloading', color: 'yellow' },
  processing: { label: 'Processing', color: 'violet' },
  completed: { label: 'Downloaded', color: 'green' },
  failed: { label: 'Failed', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' },
}

function ProgramBlock({
  program,
  onClick,
  isPast,
  isCurrent,
  isDownloadable,
  isSchedulable,
  isExpired,
  elementId,
  previousDownload,
  guideOffsetHours,
}) {
  const startTime = getChannelDisplayTime(program, 'start', guideOffsetHours)
  const endTime = getChannelDisplayTime(program, 'end', guideOffsetHours)
  const isMobile = useMediaQuery('(max-width: 48em)')
  const previousStatus = previousDownload?.status
  const previousMeta = (previousStatus && previousDownloadStatusMeta[previousStatus]) || null

  const backgroundColor = isCurrent
    ? 'var(--mantine-color-green-light)'
    : isPast && program.has_archive && !isExpired
    ? 'rgba(245, 159, 0, 0.1)'
    : isPast
    ? 'var(--mantine-color-dark-5)'
    : 'var(--mantine-color-dark-6)'

  const isClickable = isDownloadable || isSchedulable
  const actionIcon = isDownloadable ? IconDownload : isSchedulable ? IconCalendar : null
  const ActionIcon = actionIcon

  return (
    <Box
      id={elementId}
      style={{
        padding: '8px 12px',
        borderRadius: 6,
        backgroundColor,
        cursor: isClickable ? 'pointer' : 'default',
        borderTop: '1px solid transparent',
        borderRight: '1px solid transparent',
        borderBottom: '1px solid transparent',
        borderLeft: isDownloadable
          ? '3px solid #f59f00'
          : isSchedulable
          ? '3px solid var(--mantine-color-teal-6)'
          : '3px solid transparent',
        opacity: isPast && (!program.has_archive || isExpired) ? 0.5 : 1,
        transition: 'box-shadow 0.15s, background-color 0.15s',
      }}
      onClick={() => isClickable && onClick(program, { action: isDownloadable ? 'download' : 'schedule' })}
      onMouseEnter={(e) => {
        if (isClickable) {
          e.currentTarget.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.35)'
        }
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.boxShadow = 'none'
      }}
    >
      <Stack gap={4}>
        <Group justify="space-between" wrap="nowrap">
          <Group gap={6} wrap="nowrap" style={{ flex: 1, minWidth: 0 }}>
            {isCurrent && (
              <Box
                style={{
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  background: '#fa5252',
                  flexShrink: 0,
                  animation: 'mustarrd-rec-pulse 1.5s ease-in-out infinite',
                }}
              />
            )}
            <Text
              size={isMobile ? 'xs' : 'sm'}
              fw={500}
              lineClamp={isMobile ? 2 : 1}
              style={{ minWidth: 0, lineHeight: isMobile ? 1.25 : undefined }}
            >
              {program.title}
            </Text>
          </Group>
          {ActionIcon && (
            <ActionIcon size={14} style={{ flexShrink: 0 }} opacity={0.7} />
          )}
        </Group>

        <Group gap="xs">
          <Text size="xs" c="dimmed">
            {startTime?.format('h:mm A') || 'Unknown'} – {endTime?.format('h:mm A') || 'Unknown'}
          </Text>
          <Badge size="xs" variant="light" color={isCurrent ? 'green' : isPast ? 'gray' : 'yellow'}>
            {program.duration_minutes}m
          </Badge>
          {isExpired && (
            <Tooltip label="No longer available — outside this channel's catchup window" withArrow>
              <Badge size="xs" variant="light" color="gray">
                Expired
              </Badge>
            </Tooltip>
          )}
          {previousMeta && (
            <Badge size="xs" variant="light" color={previousMeta.color}>
              {previousMeta.label}
            </Badge>
          )}
        </Group>

        {program.description && (
          <Text size="xs" c="dimmed" lineClamp={2}>
            {program.description}
          </Text>
        )}
      </Stack>
    </Box>
  )
}

function DaySection({ date, programs, onProgramClick, getProgramPreviousDownload, guideOffsetHours, archiveCutoff }) {
  const now = getNowUtc()
  const displayNow = now.add(getGuideOffsetHours(guideOffsetHours), 'hour')
  const isToday = date.isSame(displayNow, 'day')

  const sortedPrograms = useMemo(() => {
    return [...programs].sort((a, b) =>
      (getProgramInstant(b, 'start')?.valueOf() || 0) - (getProgramInstant(a, 'start')?.valueOf() || 0)
    )
  }, [programs])

  return (
    <Stack gap="xs">
      <Group gap="xs" align="center">
        <Text
          fw={700}
          size="sm"
          style={isToday ? {
            color: '#f59f00',
            fontFamily: "'Bebas Neue', cursive",
            fontSize: 16,
            letterSpacing: '0.08em',
          } : {
            color: 'var(--mantine-color-dimmed)',
            textTransform: 'uppercase',
            fontSize: 11,
            letterSpacing: '0.08em',
          }}
        >
          {isToday ? 'Today' : date.format('ddd, MMM D')}
        </Text>
        <Divider style={{ flex: 1 }} />
        <Text size="xs" c="dimmed">{sortedPrograms.length}</Text>
      </Group>

      <Stack gap={6}>
        {sortedPrograms.map((program, idx) => {
          const start = getProgramInstant(program, 'start')
          const end = getProgramInstant(program, 'end')
          const isPast = Boolean(end?.isBefore(now))
          const isCurrent = Boolean(start?.isBefore(now) && end?.isAfter(now))
          const isExpired = Boolean(
            isPast && program.has_archive && archiveCutoff && end?.isBefore(archiveCutoff)
          )
          const isDownloadable = isPast && program.has_archive && !isExpired
          const isSchedulable = !isPast
          const elementId = `epg-program-${program.epg_id || program.id || idx}`
          const previousDownload = getProgramPreviousDownload
            ? getProgramPreviousDownload(program)
            : null

          return (
            <ProgramBlock
              key={program.id || idx}
              program={program}
              onClick={onProgramClick}
              isPast={isPast}
              isCurrent={isCurrent}
              isDownloadable={isDownloadable}
              isSchedulable={isSchedulable}
              isExpired={isExpired}
              elementId={elementId}
              previousDownload={previousDownload}
              guideOffsetHours={guideOffsetHours}
            />
          )
        })}
      </Stack>
    </Stack>
  )
}

export default function EPGGrid({
  epgData,
  onProgramClick,
  showFuture = false,
  getProgramPreviousDownload = null,
  guideOffsetHours = 0,
  archiveDays = null,
}) {
  const scrollAreaRef = useRef(null)
  const didAutoScrollRef = useRef(false)
  const normalizedGuideOffsetHours = getGuideOffsetHours(guideOffsetHours)
  const now = useMemo(() => getNowUtc(), [epgData, showFuture])

  // Programs that ended before this instant are outside the provider's
  // catchup window and would 404 if queued for download.
  const archiveCutoff = useMemo(() => {
    const parsed = Number(archiveDays)
    if (!Number.isFinite(parsed) || parsed <= 0) return null
    return now.subtract(parsed, 'day')
  }, [archiveDays, now])

  const visiblePrograms = useMemo(() => {
    if (!epgData) return epgData
    if (showFuture) return epgData
    return epgData.filter((program) => getProgramInstant(program, 'end')?.isBefore(now))
  }, [epgData, showFuture, now])

  const programsByDay = useMemo(() => {
    const grouped = {}

    if (!visiblePrograms) return []

    visiblePrograms.forEach((program) => {
      const date = getChannelDisplayTime(program, 'start', normalizedGuideOffsetHours)?.startOf('day')
      if (!date) return

      const key = date.format('YYYY-MM-DD')
      if (!grouped[key]) {
        grouped[key] = {
          date,
          programs: [],
        }
      }
      grouped[key].programs.push(program)
    })

    return Object.values(grouped).sort((a, b) => b.date.valueOf() - a.date.valueOf())
  }, [normalizedGuideOffsetHours, visiblePrograms])

  const scrollTargetId = useMemo(() => {
    let target = null
    for (const { programs } of programsByDay) {
      const sorted = [...programs].sort((a, b) =>
        (getProgramInstant(b, 'start')?.valueOf() || 0) - (getProgramInstant(a, 'start')?.valueOf() || 0)
      )
      for (const program of sorted) {
        const start = getProgramInstant(program, 'start')
        const end = getProgramInstant(program, 'end')
        const isCurrent = Boolean(start?.isBefore(now) && end?.isAfter(now))
        if (isCurrent) {
          target = program
          break
        }
      }
      if (target) break
    }

    if (!target) {
      for (const { programs } of programsByDay) {
        const sorted = [...programs].sort((a, b) =>
          (getProgramInstant(b, 'start')?.valueOf() || 0) - (getProgramInstant(a, 'start')?.valueOf() || 0)
        )
        for (const program of sorted) {
          const end = getProgramInstant(program, 'end')
          if (end?.isBefore(now)) {
            target = program
            break
          }
        }
        if (target) break
      }
    }

    return target ? `epg-program-${target.epg_id || target.id || 'target'}` : null
  }, [now, programsByDay])

  useEffect(() => {
    didAutoScrollRef.current = false
  }, [epgData, showFuture])

  useEffect(() => {
    if (!scrollTargetId || didAutoScrollRef.current) return
    const target = document.getElementById(scrollTargetId)
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      didAutoScrollRef.current = true
    }
  }, [scrollTargetId])

  if (!visiblePrograms || visiblePrograms.length === 0) {
    return (
      <Stack align="center" justify="center" h={200}>
        <IconClock size={48} opacity={0.3} />
        <Text c="dimmed">No EPG data available</Text>
      </Stack>
    )
  }

  const downloadableCount = visiblePrograms.filter((p) => {
    const end = getProgramInstant(p, 'end')
    if (!p.has_archive || !end?.isBefore(now)) return false
    return !(archiveCutoff && end.isBefore(archiveCutoff))
  }).length
  const schedulableCount = visiblePrograms.filter(
    (p) => getProgramInstant(p, 'end')?.isAfter(now)
  ).length

  return (
    <Stack gap="md" style={{ height: '100%', minHeight: 0 }}>
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          Showing {visiblePrograms.length} programs
        </Text>
        <Group gap="xs">
          <Badge variant="light" color="yellow" leftSection={<IconDownload size={12} />}>
            {downloadableCount} available
          </Badge>
          <Badge variant="light" color="teal" leftSection={<IconCalendar size={12} />}>
            {schedulableCount} upcoming
          </Badge>
        </Group>
      </Group>

      <ScrollArea style={{ flex: 1, minHeight: 0 }} ref={scrollAreaRef}>
        <Stack gap="lg">
          {programsByDay.map(({ date, programs }) => (
            <Box key={date.format('YYYY-MM-DD')} id={`epg-day-${date.format('YYYY-MM-DD')}`}>
              <DaySection
                date={date}
                programs={programs}
                onProgramClick={onProgramClick}
                getProgramPreviousDownload={getProgramPreviousDownload}
                guideOffsetHours={normalizedGuideOffsetHours}
                archiveCutoff={archiveCutoff}
              />
            </Box>
          ))}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}
