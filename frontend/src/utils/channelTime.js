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

// The helpers above all go one way: a UTC instant out of the provider, shown on
// the channel's clock. A time slot is typed rather than read, so it needs the
// reverse — a wall-clock time the user entered against the guide, turned back
// into the UTC instant the provider is asked for.

const CHANNEL_INPUT_FORMAT = 'YYYY-MM-DDTHH:mm'

/**
 * Parse a datetime-local string entered on the channel's clock into a UTC instant.
 * Returns null when the value is absent or unparseable.
 */
export function channelDisplayToUtc(value, guideOffsetHours = 0) {
  if (!value) {
    return null
  }
  const parsed = dayjs.utc(value)
  if (!parsed.isValid()) {
    return null
  }
  return parsed.subtract(getGuideOffsetHours(guideOffsetHours), 'hour')
}

/** As channelDisplayToUtc, but as a unix timestamp in seconds. */
export function channelDisplayToUnix(value, guideOffsetHours = 0) {
  const instant = channelDisplayToUtc(value, guideOffsetHours)
  return instant ? instant.unix() : null
}

/** Format a UTC instant as a datetime-local value on the channel's clock. */
export function utcToChannelInput(instant, guideOffsetHours = 0) {
  if (!instant) {
    return ''
  }
  return dayjs.utc(instant).add(getGuideOffsetHours(guideOffsetHours), 'hour').format(CHANNEL_INPUT_FORMAT)
}

/** Now on the channel's clock, rounded down, as a datetime-local value. */
export function nowChannelInput(guideOffsetHours = 0, roundToMinutes = 15) {
  const now = getNowUtc().add(getGuideOffsetHours(guideOffsetHours), 'hour')
  const step = Math.max(1, roundToMinutes)
  const rounded = now.minute(Math.floor(now.minute() / step) * step).second(0).millisecond(0)
  return rounded.format(CHANNEL_INPUT_FORMAT)
}

/** Shift a datetime-local value by minutes, staying on the channel's clock. */
export function addMinutesToChannelInput(value, minutes) {
  if (!value) {
    return ''
  }
  const parsed = dayjs.utc(value)
  return parsed.isValid() ? parsed.add(minutes, 'minute').format(CHANNEL_INPUT_FORMAT) : ''
}

export function formatAirDateTime(program, kind = 'start', guideOffsetHours = 0) {
  const displayTime = getChannelDisplayTime(program, kind, guideOffsetHours)
  if (!displayTime) return ''
  const offset = getGuideOffsetHours(guideOffsetHours)
  const now = getNowUtc().add(offset, 'hour')
  const todayDate = now.format('YYYY-MM-DD')
  const tomorrowDate = now.add(1, 'day').format('YYYY-MM-DD')
  const yesterdayDate = now.subtract(1, 'day').format('YYYY-MM-DD')
  const displayDate = displayTime.format('YYYY-MM-DD')
  const timeStr = displayTime.format('h:mm A')
  if (displayDate === todayDate) {
    return `Today at ${timeStr}`
  }
  if (displayDate === tomorrowDate) {
    return `Tomorrow at ${timeStr}`
  }
  if (displayDate === yesterdayDate) {
    return `Yesterday at ${timeStr}`
  }
  for (let d = 2; d <= 6; d++) {
    if (displayDate === now.add(d, 'day').format('YYYY-MM-DD')) {
      return `${displayTime.format('dddd')} at ${timeStr}`
    }
  }
  return `${displayTime.format('ddd, MMM D, YYYY')} at ${timeStr}`
}
