import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  formatAirDateTime,
  formatChannelDateTime,
  getChannelDisplayTime,
  getGuideOffsetHours,
  getProgramInstant,
} from './channelTime'

describe('getGuideOffsetHours', () => {
  it('returns numeric values as-is', () => {
    expect(getGuideOffsetHours(2)).toBe(2)
    expect(getGuideOffsetHours(-5.5)).toBe(-5.5)
  })

  it('coerces numeric strings', () => {
    expect(getGuideOffsetHours('3')).toBe(3)
  })

  it('falls back to 0 for non-numeric input', () => {
    expect(getGuideOffsetHours('abc')).toBe(0)
    expect(getGuideOffsetHours(undefined)).toBe(0)
    expect(getGuideOffsetHours(null)).toBe(0)
  })
})

describe('getProgramInstant', () => {
  it('returns null for missing program', () => {
    expect(getProgramInstant(null)).toBeNull()
    expect(getProgramInstant(undefined)).toBeNull()
  })

  it('prefers start_timestamp over ISO fields', () => {
    const program = {
      start_timestamp: 1750000000, // 2025-06-15T15:06:40Z
      start_time: '2020-01-01T00:00:00Z',
    }
    const instant = getProgramInstant(program, 'start')
    expect(instant.unix()).toBe(1750000000)
  })

  it('falls back to ISO start_time when timestamp is invalid', () => {
    const program = {
      start_timestamp: 'not-a-number',
      start_time: '2026-06-01T12:00:00Z',
    }
    const instant = getProgramInstant(program, 'start')
    expect(instant.format('YYYY-MM-DD HH:mm')).toBe('2026-06-01 12:00')
  })

  it('ignores zero and negative timestamps', () => {
    expect(getProgramInstant({ start_timestamp: 0 })).toBeNull()
    expect(getProgramInstant({ start_timestamp: -100 })).toBeNull()
  })

  it('uses stop_timestamp and end_time for kind=end', () => {
    const fromTimestamp = getProgramInstant({ stop_timestamp: 1750003600 }, 'end')
    expect(fromTimestamp.unix()).toBe(1750003600)

    const fromIso = getProgramInstant({ end_time: '2026-06-01T13:30:00Z' }, 'end')
    expect(fromIso.format('HH:mm')).toBe('13:30')
  })

  it('uses available_at first for kind=available, then end_time', () => {
    const program = {
      available_at: '2026-06-01T14:00:00Z',
      end_time: '2026-06-01T13:00:00Z',
    }
    expect(getProgramInstant(program, 'available').format('HH:mm')).toBe('14:00')
    expect(getProgramInstant({ end_time: '2026-06-01T13:00:00Z' }, 'available').format('HH:mm')).toBe('13:00')
  })

  it('returns null when nothing parseable is present', () => {
    expect(getProgramInstant({ title: 'News' })).toBeNull()
  })
})

describe('getChannelDisplayTime', () => {
  it('applies the guide offset in hours', () => {
    const program = { start_time: '2026-06-01T12:00:00Z' }
    expect(getChannelDisplayTime(program, 'start', 0).format('HH:mm')).toBe('12:00')
    expect(getChannelDisplayTime(program, 'start', 2).format('HH:mm')).toBe('14:00')
    expect(getChannelDisplayTime(program, 'start', -3).format('HH:mm')).toBe('09:00')
  })

  it('returns null when the program has no time', () => {
    expect(getChannelDisplayTime({}, 'start', 2)).toBeNull()
  })
})

describe('formatChannelDateTime', () => {
  it('formats with the default pattern', () => {
    const program = { start_time: '2026-06-01T12:05:00Z' }
    expect(formatChannelDateTime(program)).toBe('Jun 1, 2026 12:05 PM')
  })

  it('accepts a custom format', () => {
    const program = { start_time: '2026-06-01T12:05:00Z' }
    expect(formatChannelDateTime(program, 'start', 0, 'YYYY/MM/DD')).toBe('2026/06/01')
  })

  it('returns empty string when no time is available', () => {
    expect(formatChannelDateTime({})).toBe('')
  })
})

describe('formatAirDateTime', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    // Tuesday 2026-06-09 18:00 UTC
    vi.setSystemTime(new Date('2026-06-09T18:00:00Z'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('labels programs airing today', () => {
    const program = { start_time: '2026-06-09T20:30:00Z' }
    expect(formatAirDateTime(program)).toBe('Today at 8:30 PM')
  })

  it('labels tomorrow and yesterday', () => {
    expect(formatAirDateTime({ start_time: '2026-06-10T09:00:00Z' })).toBe('Tomorrow at 9:00 AM')
    expect(formatAirDateTime({ start_time: '2026-06-08T09:00:00Z' })).toBe('Yesterday at 9:00 AM')
  })

  it('uses the weekday name within the next six days', () => {
    // 2026-06-12 is a Friday, three days out
    expect(formatAirDateTime({ start_time: '2026-06-12T21:00:00Z' })).toBe('Friday at 9:00 PM')
  })

  it('falls back to the full date outside the weekday window', () => {
    expect(formatAirDateTime({ start_time: '2026-06-20T21:00:00Z' })).toBe('Sat, Jun 20, 2026 at 9:00 PM')
  })

  it('shifts the today boundary by the guide offset', () => {
    // 23:30 UTC today becomes 01:30 tomorrow with a +2h guide offset,
    // and "now" shifts to 20:00, so the program still displays as tomorrow.
    const program = { start_time: '2026-06-09T23:30:00Z' }
    expect(formatAirDateTime(program, 'start', 2)).toBe('Tomorrow at 1:30 AM')
  })

  it('returns empty string when the program has no time', () => {
    expect(formatAirDateTime({})).toBe('')
  })
})
