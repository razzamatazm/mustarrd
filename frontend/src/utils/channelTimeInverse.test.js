import { afterEach, describe, expect, it, vi } from 'vitest'
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc.js'
import {
  addMinutesToChannelInput,
  channelDisplayToUnix,
  channelDisplayToUtc,
  nowChannelInput,
  utcToChannelInput,
} from './channelTime'

dayjs.extend(utc)

// Every other helper here converts a provider instant into something to show.
// A time slot is typed instead of read, so these go the other way, and a wrong
// offset silently records the wrong hour.

describe('channelDisplayToUtc', () => {
  it('treats the entered time as the channel clock when there is no offset', () => {
    const instant = channelDisplayToUtc('2026-08-15T14:00', 0)
    expect(instant.toISOString()).toBe('2026-08-15T14:00:00.000Z')
  })

  it('subtracts a positive guide offset', () => {
    // The guide runs +2, so 2pm on screen is noon UTC.
    const instant = channelDisplayToUtc('2026-08-15T14:00', 2)
    expect(instant.toISOString()).toBe('2026-08-15T12:00:00.000Z')
  })

  it('adds back a negative guide offset', () => {
    const instant = channelDisplayToUtc('2026-08-15T14:00', -5)
    expect(instant.toISOString()).toBe('2026-08-15T19:00:00.000Z')
  })

  it('handles an offset that crosses a date boundary', () => {
    const instant = channelDisplayToUtc('2026-08-15T01:00', 3)
    expect(instant.toISOString()).toBe('2026-08-14T22:00:00.000Z')
  })

  it('returns null for empty or unparseable input', () => {
    expect(channelDisplayToUtc('', 0)).toBeNull()
    expect(channelDisplayToUtc(null, 0)).toBeNull()
    expect(channelDisplayToUtc('not a date', 0)).toBeNull()
  })
})

describe('channelDisplayToUnix', () => {
  it('returns seconds since the epoch', () => {
    const expected = dayjs.utc('2026-08-15T12:00:00Z').unix()
    expect(channelDisplayToUnix('2026-08-15T14:00', 2)).toBe(expected)
  })

  it('returns null when there is nothing to parse', () => {
    expect(channelDisplayToUnix('', 0)).toBeNull()
  })
})

describe('round tripping', () => {
  it('utcToChannelInput reverses channelDisplayToUtc', () => {
    for (const offset of [0, 2, -5, 5.5]) {
      const entered = '2026-08-15T14:00'
      const instant = channelDisplayToUtc(entered, offset)
      expect(utcToChannelInput(instant, offset)).toBe(entered)
    }
  })

  it('utcToChannelInput shows a provider instant on the channel clock', () => {
    const instant = dayjs.utc('2026-08-15T12:00:00Z')
    expect(utcToChannelInput(instant, 2)).toBe('2026-08-15T14:00')
  })

  it('returns an empty string for a missing instant', () => {
    expect(utcToChannelInput(null, 0)).toBe('')
  })
})

describe('nowChannelInput', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('rounds down to the nearest quarter hour on the channel clock', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-15T14:07:31Z'))
    expect(nowChannelInput(0, 15)).toBe('2026-08-15T14:00')
  })

  it('applies the guide offset before rounding', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-15T14:52:00Z'))
    expect(nowChannelInput(2, 15)).toBe('2026-08-15T16:45')
  })

  it('handles an offset that pushes into the next day', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-15T23:20:00Z'))
    expect(nowChannelInput(3, 15)).toBe('2026-08-16T02:15')
  })
})

describe('addMinutesToChannelInput', () => {
  it('adds minutes without leaving the channel clock', () => {
    expect(addMinutesToChannelInput('2026-08-15T14:00', 60)).toBe('2026-08-15T15:00')
  })

  it('crosses midnight correctly', () => {
    expect(addMinutesToChannelInput('2026-08-15T23:30', 60)).toBe('2026-08-16T00:30')
  })

  it('returns an empty string for missing input', () => {
    expect(addMinutesToChannelInput('', 60)).toBe('')
  })
})
