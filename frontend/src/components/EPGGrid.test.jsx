import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import dayjs from 'dayjs'

import EPGGrid from './EPGGrid'

const pastProgram = {
  id: 'p1',
  epg_id: 'p1',
  title: 'Archived Show',
  description: 'A show inside the catchup window',
  duration_minutes: 60,
  has_archive: true,
  start_time: dayjs().subtract(3, 'hour').toISOString(),
  end_time: dayjs().subtract(2, 'hour').toISOString(),
}

const currentProgram = {
  id: 'p2',
  epg_id: 'p2',
  title: 'Live Broadcast',
  duration_minutes: 60,
  has_archive: true,
  start_time: dayjs().subtract(30, 'minute').toISOString(),
  end_time: dayjs().add(30, 'minute').toISOString(),
}

const futureProgram = {
  id: 'p3',
  epg_id: 'p3',
  title: 'Tomorrow Special',
  duration_minutes: 30,
  has_archive: true,
  start_time: dayjs().add(1, 'day').toISOString(),
  end_time: dayjs().add(1, 'day').add(30, 'minute').toISOString(),
}

const expiredProgram = {
  id: 'p4',
  epg_id: 'p4',
  title: 'Too Old Show',
  duration_minutes: 60,
  has_archive: true,
  start_time: dayjs().subtract(10, 'day').toISOString(),
  end_time: dayjs().subtract(10, 'day').add(1, 'hour').toISOString(),
}

function renderGrid(props = {}) {
  const onProgramClick = vi.fn()
  render(
    <MantineProvider>
      <EPGGrid epgData={[pastProgram]} onProgramClick={onProgramClick} {...props} />
    </MantineProvider>
  )
  return { onProgramClick }
}

describe('EPGGrid', () => {
  it('renders downloadable rows with Preview and Download actions', () => {
    const { onProgramClick } = renderGrid()
    expect(screen.getByText('Archived Show')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Download/ }))
    expect(onProgramClick.mock.calls[0][1]).toEqual({ action: 'download' })
    fireEvent.click(screen.getByRole('button', { name: /Preview/ }))
    expect(onProgramClick.mock.calls[1][1]).toEqual({ action: 'preview', mode: 'catchup' })
  })

  it('marks currently airing programs with a quiet In progress badge', () => {
    const { onProgramClick } = renderGrid({
      epgData: [currentProgram],
      showFuture: true,
    })
    expect(screen.getByText('In progress')).toBeInTheDocument()
    // No schedule/download buttons on a live row, only preview
    expect(screen.queryByRole('button', { name: /Schedule/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Download/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Preview/ }))
    expect(onProgramClick.mock.calls[0][1]).toEqual({ action: 'preview', mode: 'live' })
  })

  it('shows Schedule on future programs only when showFuture is on', () => {
    renderGrid({ epgData: [futureProgram], showFuture: true })
    expect(screen.getByRole('button', { name: /Schedule/ })).toBeInTheDocument()
  })

  it('hides future programs when showFuture is off', () => {
    renderGrid({ epgData: [futureProgram], showFuture: false })
    expect(screen.queryByText('Tomorrow Special')).not.toBeInTheDocument()
  })

  it('marks programs outside the archive window as expired', () => {
    renderGrid({ epgData: [expiredProgram], archiveDays: 7 })
    expect(screen.getByText('Expired')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Download/ })).not.toBeInTheDocument()
  })

  it('renders sticky day headers with TODAY styling for the current day', () => {
    // Day grouping happens in UTC; "3 hours ago" lands on yesterday when the
    // local evening has crossed UTC midnight. Pin the program to UTC today.
    const utcMidnight = `${new Date().toISOString().slice(0, 10)}T00:00:00.000Z`
    const todayProgram = {
      ...pastProgram,
      start_time: utcMidnight,
      end_time: `${new Date().toISOString().slice(0, 10)}T00:01:00.000Z`,
    }
    renderGrid({ epgData: [todayProgram], showFuture: true })
    expect(screen.getByText('TODAY')).toBeInTheDocument()
  })
})
