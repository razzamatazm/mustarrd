import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import dayjs from 'dayjs'

import Scheduled from './Scheduled'

// Freeze the clock at midday before the relative fixtures below are built.
// The day-grouping ("TONIGHT" = starts today) compares against the real clock,
// so a fixture of `now + 2h` built near midnight would slip into tomorrow and
// drop the TONIGHT header — a flake that only fired when CI ran close to UTC
// midnight. Pinning to noon keeps every offset on a predictable calendar day.
// shouldAdvanceTime lets Testing Library's waitFor still progress.
vi.useFakeTimers({ shouldAdvanceTime: true })
vi.setSystemTime(new Date('2026-06-23T12:00:00Z'))
afterAll(() => {
  vi.useRealTimers()
})

vi.mock('../api', () => ({
  schedulesApi: {
    list: vi.fn(),
    cancel: vi.fn(),
    exportAll: vi.fn(),
    importDoc: vi.fn(),
  },
  recordingRulesApi: {
    list: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
  },
  accountsApi: { publicList: vi.fn() },
  authApi: { status: vi.fn() },
  settingsApi: { get: vi.fn(), update: vi.fn() },
  downloadsApi: { retry: vi.fn() },
}))

const tonightSchedule = {
  id: 1,
  status: 'scheduled',
  program_title: 'Evening Quiz',
  channel_name: 'Game Network',
  account_id: 1,
  duration_minutes: 60,
  program_start: dayjs().add(2, 'hour').toISOString(),
  program_end: dayjs().add(3, 'hour').toISOString(),
}

const tomorrowSchedule = {
  id: 2,
  status: 'paused_low_space',
  program_title: 'Morning Cartoons',
  channel_name: 'Kids TV',
  account_id: 1,
  duration_minutes: 30,
  program_start: dayjs().add(1, 'day').toISOString(),
  program_end: dayjs().add(1, 'day').add(30, 'minute').toISOString(),
}

const failedSchedule = {
  id: 3,
  status: 'failed',
  program_title: 'Old Movie Night',
  channel_name: 'Cinema Plus',
  account_id: 1,
  duration_minutes: 120,
  program_start: dayjs().subtract(2, 'day').toISOString(),
  program_end: dayjs().subtract(2, 'day').add(2, 'hour').toISOString(),
  download_status: 'failed',
  download_id: 9,
  download_error_message: 'Stream unavailable',
}

async function renderScheduled({ schedules = [], rules = [], isAdmin = true, initialEntries = ['/scheduled'] } = {}) {
  const { schedulesApi, recordingRulesApi, accountsApi, authApi, settingsApi } = await import('../api')
  schedulesApi.list.mockResolvedValue(schedules)
  recordingRulesApi.list.mockResolvedValue(rules)
  accountsApi.publicList.mockResolvedValue([{ id: 1, name: 'Main', guide_offset_hours: 0 }])
  authApi.status.mockResolvedValue({ is_admin: isAdmin })
  settingsApi.get.mockResolvedValue({ auto_retry_failed_downloads: true })

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })

  render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider>
        <MemoryRouter initialEntries={initialEntries}>
          <Scheduled />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  )

  await waitFor(() => {
    expect(screen.getByText('Scheduled')).toBeInTheDocument()
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Scheduled page', () => {
  it('groups upcoming recordings by day with a TONIGHT header for today', async () => {
    await renderScheduled({ schedules: [tonightSchedule, tomorrowSchedule] })

    expect(await screen.findByText('TONIGHT')).toBeInTheDocument()
    expect(screen.getByText(dayjs().add(1, 'day').format('ddd, MMM D'))).toBeInTheDocument()
    expect(screen.getByText('Evening Quiz')).toBeInTheDocument()
    expect(screen.getByText('Morning Cartoons')).toBeInTheDocument()
    // Plain scheduled rows have no status badge; non-default statuses do
    expect(screen.queryByText('Scheduled', { selector: '.mantine-Badge-label' })).not.toBeInTheDocument()
    expect(screen.getByText('Paused — low space')).toBeInTheDocument()
  })

  it('asks for confirmation before cancelling an upcoming recording', async () => {
    const { schedulesApi } = await import('../api')
    schedulesApi.cancel.mockResolvedValue({})
    await renderScheduled({ schedules: [tonightSchedule] })

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel recording' }))
    expect(screen.getByText('Cancel this recording?')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Yes, cancel' }))
    await waitFor(() => {
      expect(schedulesApi.cancel.mock.calls[0][0]).toBe(1)
    })
  })

  it('shows history as compact rows with retry for failed recordings', async () => {
    await renderScheduled({
      schedules: [failedSchedule],
      initialEntries: ['/scheduled?tab=history'],
    })

    expect(await screen.findByText('Old Movie Night')).toBeInTheDocument()
    expect(screen.getByText(/Stream unavailable/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Failed' })).toBeInTheDocument()
  })

  it('shows the auto-retry switch to admins only', async () => {
    await renderScheduled({ schedules: [], isAdmin: false })
    expect(screen.queryByLabelText('Auto-retry failed downloads')).not.toBeInTheDocument()
  })

  it('lists, disables, and deletes recurring recording rules', async () => {
    const { recordingRulesApi } = await import('../api')
    recordingRulesApi.update.mockResolvedValue({})
    recordingRulesApi.delete.mockResolvedValue({ status: 'deleted' })
    await renderScheduled({
      rules: [{
        id: 7,
        account_id: 1,
        channel_name: 'Game Network',
        title_match: 'Evening Quiz',
        enabled: true,
        delete_after_days: 14,
      }],
      initialEntries: ['/scheduled?tab=rules'],
    })

    expect(await screen.findByText('Evening Quiz')).toBeInTheDocument()
    expect(screen.getByText(/Deletes recordings after 14 days/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('switch', { name: 'Enable Evening Quiz' }))
    await waitFor(() => {
      expect(recordingRulesApi.update).toHaveBeenCalledWith(7, { enabled: false })
    })

    fireEvent.click(screen.getByRole('button', { name: 'Delete rule Evening Quiz' }))
    fireEvent.click(screen.getByRole('button', { name: 'Yes, delete' }))
    await waitFor(() => {
      expect(recordingRulesApi.delete).toHaveBeenCalledWith(7)
    })
  })

  it('toggles the global rule retention setting', async () => {
    const { settingsApi } = await import('../api')
    settingsApi.update.mockResolvedValue({})
    await renderScheduled({ rules: [], initialEntries: ['/scheduled?tab=rules'] })

    fireEvent.click(await screen.findByLabelText('Enable rule-based retention cleanup'))
    await waitFor(() => {
      expect(settingsApi.update).toHaveBeenCalledWith({ recording_rule_retention_enabled: true })
    })
  })

  it('hides the retention toggle from non-admins', async () => {
    await renderScheduled({ rules: [], isAdmin: false, initialEntries: ['/scheduled?tab=rules'] })
    await screen.findByText(/No recurring recording rules yet/)
    expect(screen.queryByLabelText('Enable rule-based retention cleanup')).not.toBeInTheDocument()
  })

  it('edits recurring matching, padding, days, and deletion retention', async () => {
    const { recordingRulesApi } = await import('../api')
    recordingRulesApi.update.mockResolvedValue({ scheduled_count: 0 })
    await renderScheduled({
      rules: [{
        id: 8,
        account_id: 1,
        channel_name: 'Game Network',
        title_match: 'Evening Quiz',
        match_mode: 'exact',
        enabled: true,
        days_of_week: null,
        delete_after_days: null,
        pre_padding_minutes: 1,
        post_padding_minutes: 5,
      }],
      initialEntries: ['/scheduled?tab=rules'],
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Edit rule Evening Quiz' }))
    fireEvent.change(await screen.findByLabelText('Title match'), { target: { value: 'Quiz' } })
    fireEvent.change(screen.getByLabelText('Delete completed recordings after'), { target: { value: '14' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save rule' }))

    await waitFor(() => {
      expect(recordingRulesApi.update).toHaveBeenCalledWith(8, expect.objectContaining({
        title_match: 'Quiz',
        match_mode: 'exact',
        delete_after_days: 14,
        pre_padding_minutes: 1,
        post_padding_minutes: 5,
      }))
    })
  })
})
