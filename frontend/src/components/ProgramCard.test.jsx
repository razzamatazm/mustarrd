import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithMantine } from '../test/renderWithProviders'
import ProgramCard from './ProgramCard'

const NOW = new Date('2026-06-09T18:00:00Z')
const HOUR = 3600

function unixHoursFromNow(hours) {
  return Math.floor(NOW.getTime() / 1000) + hours * HOUR
}

function makeProgram(overrides = {}) {
  return {
    title: 'Evening News',
    description: 'Daily headlines',
    duration_minutes: 60,
    has_archive: true,
    start_timestamp: unixHoursFromNow(-3),
    stop_timestamp: unixHoursFromNow(-2),
    ...overrides,
  }
}

describe('ProgramCard', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows title, channel name and duration', () => {
    renderWithMantine(
      <ProgramCard program={makeProgram()} channel={{ name: 'BBC One' }} />
    )
    expect(screen.getByText('Evening News')).toBeInTheDocument()
    expect(screen.getByText('BBC One')).toBeInTheDocument()
    expect(screen.getByText('60m')).toBeInTheDocument()
    expect(screen.getByText('Daily headlines')).toBeInTheDocument()
  })

  it('marks a past archived program as Available and clickable', () => {
    const onClick = vi.fn()
    const program = makeProgram()
    renderWithMantine(<ProgramCard program={program} onClick={onClick} />)

    expect(screen.getByText('Available')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Evening News'))
    expect(onClick).toHaveBeenCalledWith(program)
  })

  it('does not fire onClick for a past program without archive', () => {
    const onClick = vi.fn()
    renderWithMantine(
      <ProgramCard program={makeProgram({ has_archive: false })} onClick={onClick} />
    )

    expect(screen.queryByText('Available')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Evening News'))
    expect(onClick).not.toHaveBeenCalled()
  })

  it('shows Now Playing for a currently airing program and is not downloadable', () => {
    const onClick = vi.fn()
    renderWithMantine(
      <ProgramCard
        program={makeProgram({
          start_timestamp: unixHoursFromNow(-1),
          stop_timestamp: unixHoursFromNow(1),
        })}
        onClick={onClick}
      />
    )

    expect(screen.getByText('Now Playing')).toBeInTheDocument()
    expect(screen.queryByText('Available')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('Evening News'))
    expect(onClick).not.toHaveBeenCalled()
  })

  it('renders the time range shifted by the guide offset', () => {
    renderWithMantine(
      <ProgramCard
        program={makeProgram({
          start_timestamp: Math.floor(new Date('2026-06-09T15:00:00Z').getTime() / 1000),
          stop_timestamp: Math.floor(new Date('2026-06-09T16:00:00Z').getTime() / 1000),
        })}
        guideOffsetHours={2}
      />
    )

    expect(screen.getByText(/5:00 PM - 6:00 PM/)).toBeInTheDocument()
  })

  it('falls back to Unknown when times are missing', () => {
    renderWithMantine(
      <ProgramCard
        program={{ title: 'Mystery', duration_minutes: 30, has_archive: false }}
      />
    )

    expect(screen.getByText(/Unknown - Unknown/)).toBeInTheDocument()
  })
})
