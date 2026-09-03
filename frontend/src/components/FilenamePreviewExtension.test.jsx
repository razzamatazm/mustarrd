import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithQuery } from '../test/renderWithProviders'

const previewFilename = vi.fn()

vi.mock('../api', () => ({
  downloadsApi: {
    previewFilename: (...args) => previewFilename(...args),
    create: vi.fn(),
  },
  schedulesApi: {
    create: vi.fn(),
  },
  settingsApi: {
    getPublic: vi.fn().mockResolvedValue({
      default_pre_padding_minutes: 1,
      default_post_padding_minutes: 5,
      scheduled_download_delay_minutes: 7,
    }),
  },
}))

vi.mock('@mantine/notifications', () => ({
  notifications: { show: vi.fn() },
}))

const DownloadModal = (await import('./DownloadModal')).default
const ScheduleModal = (await import('./ScheduleModal')).default

const program = {
  title: 'Breaking Bad S02E05 - Breakage',
  description: '',
  duration_minutes: 60,
  start_time: '2026-08-18T20:00:00Z',
  end_time: '2026-08-18T21:00:00Z',
}
const channel = { stream_id: 42, name: 'AMC' }

beforeEach(() => {
  previewFilename.mockReset()
  previewFilename.mockResolvedValue({
    filename: 'TV.ts/Breaking Bad/Episode.ts',
    detected_type: 'tv_show',
  })
})

describe('recording filename previews', () => {
  it('removes only the final .ts suffix in DownloadModal', async () => {
    renderWithQuery(
      <DownloadModal opened onClose={() => {}} program={program} channel={channel} accountId="1" />
    )

    expect(await screen.findByDisplayValue('TV.ts/Breaking Bad/Episode')).toBeTruthy()
  })

  it('removes only the final .ts suffix in ScheduleModal', async () => {
    renderWithQuery(
      <ScheduleModal opened onClose={() => {}} program={program} channel={channel} accountId="1" />
    )

    expect(await screen.findByDisplayValue('TV.ts/Breaking Bad/Episode')).toBeTruthy()
  })

  it('includes the scheduled download delay in the expected start time', async () => {
    renderWithQuery(
      <ScheduleModal opened onClose={() => {}} program={program} channel={channel} accountId="1" />
    )

    expect(
      await screen.findByText(/Expected start: Aug 18, 2026 9:12 PM\./)
    ).toBeTruthy()
  })
})
