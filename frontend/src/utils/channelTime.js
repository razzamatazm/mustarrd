import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc.js'

dayjs.extend(utc)

function coerceTimestamp(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return null
  }
  return Math.trunc(numeric)
}

function parseIsoUtc(value) {
  if (!value) {
    return null
  }
  const parsed = dayjs.utc(value)
  return parsed.isValid() ? parsed : null
}

export function getGuideOffsetHours(value) {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : 0
}

export function getProgramInstant(program, kind = 'start') {
  if (!program) {
    return null
  }

  let timestampKeys = ['start_timestamp']
  let isoKeys = ['start_time', 'program_start']

  if (kind === 'end') {
    timestampKeys = ['stop_timestamp']
    isoKeys = ['end_time', 'program_end']
  } else if (kind === 'available') {
    timestampKeys = []
    isoKeys = ['available_at', 'end_time', 'program_end']
  }

  for (const key of timestampKeys) {
    const timestamp = coerceTimestamp(program[key])
    if (timestamp != null) {
      return dayjs.unix(timestamp).utc()
    }
  }

  for (const key of isoKeys) {
    const parsed = parseIsoUtc(program[key])
    if (parsed) {
      return parsed
    }
  }

  return null
}

export function getChannelDisplayTime(program, kind = 'start', guideOffsetHours = 0) {
  const instant = getProgramInstant(program, kind)
  if (!instant) {
    return null
  }
  return instant.add(getGuideOffsetHours(guideOffsetHours), 'hour')
}

export function formatChannelDateTime(
  program,
  kind = 'start',
  guideOffsetHours = 0,
  format = 'MMM D, YYYY h:mm A'
) {
  const displayTime = getChannelDisplayTime(program, kind, guideOffsetHours)
  return displayTime ? displayTime.format(format) : ''
}

export function getNowUtc() {
  return dayjs.utc()
}

export function formatAirDateTime(program, kind = 'start', guideOffsetHours = 0) {
  const displayTime = getChannelDisplayTime(program, kind, guideOffsetHours)
  if (!displayTime) return ''
  const offset = getGuideOffsetHours(guideOffsetHours)
  const now = getNowUtc().add(offset, 'hour')
  const todayDate = now.format('YYYY-MM-DD')
  const tomorrowDate = now.add(1, 'day').format('YYYY-MM-DD')
  const displayDate = displayTime.format('YYYY-MM-DD')
  const timeStr = displayTime.format('h:mm A')
  if (displayDate === todayDate) {
    return `Today at ${timeStr}`
  }
  if (displayDate === tomorrowDate) {
    return `Tomorrow at ${timeStr}`
  }
  return `${displayTime.format('ddd, MMM D, YYYY')} at ${timeStr}`
}
