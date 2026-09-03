import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import dayjs from 'dayjs'

import Downloads from './Downloads'
import { downloadsApi } from '../api'

vi.mock('../api', () => ({
  downloadsApi: {
    list: vi.fn(),
    diskSpace: vi.fn(),
    cancel: vi.fn(),
    retry: vi.fn(),
    clearFinished: vi.fn(),
  },
  authApi: { status: vi.fn() },
  accountsApi: { publicList: vi.fn() },
  createDownloadWebSocket: vi.fn(() => ({ close: vi.fn() })),
}))

const activeDownload = {
  id: 1,
  status: 'downloading',
  program_title: 'Evening News',
  channel_name: 'News 24',
  account_id: 1,
  duration_minutes: 60,
  start_time: dayjs().subtract(2, 'hour').toISOString(),
  end_time: dayjs().subtract(1, 'hour').toISOString(),
  download_progress: 42,
  comskip_progress: null,
  transcode_progress: null,
  downloaded_bytes: 1024 * 1024 * 500,
  file_size: 1024 * 1024 * 1000,
}

const completedDownload = {
  id: 2,
  status: 'completed',
  program_title: 'Nature Documentary',
  channel_name: 'Discovery',
  account_id: 1,
  duration_minutes: 45,
  start_time: dayjs().subtract(1, 'day').toISOString(),
  end_time: dayjs().subtract(1, 'day').add(45, 'minute').toISOString(),
  output_path: '/completed/nature-doc.mkv',
  file_size: 1024 * 1024 * 2048,
}

const failedDownload = {
  id: 3,
  status: 'failed',
  program_title: 'Late Show',
  channel_name: 'Comedy One',
  account_id: 1,
  duration_minutes: 30,
  start_time: dayjs().subtract(2, 'day').toISOString(),
  end_time: dayjs().subtract(2, 'day').add(30, 'minute').toISOString(),
  error_message: 'Provider returned 404',
}

const interruptedDownload = {
  id: 4,
  status: 'interrupted',
  program_title: 'Championship Final',
  channel_name: 'Sports HD',
  account_id: 1,
  duration_minutes: 60,
  recorded_duration_seconds: 3240,
  start_time: dayjs().subtract(3, 'day').toISOString(),
  end_time: dayjs().subtract(3, 'day').add(60, 'minute').toISOString(),
  output_path: '/completed/championship-final.mkv',
  file_size: 1024 * 1024 * 900,
  interruption_reason: 'Connection to the provider was lost.',
}

async function renderDownloads({ downloads = [], initialEntries = ['/downloads'] } = {}) {
  const { downloadsApi, authApi, accountsApi } = await import('../api')
  downloadsApi.list.mockResolvedValue(downloads)
  downloadsApi.diskSpace.mockResolvedValue({ disk_free_gb: 412, is_low: false, available: true })
  authApi.status.mockResolvedValue({ is_admin: true })
  accountsApi.publicList.mockResolvedValue([{ id: 1, name: 'Main', guide_offset_hours: 0 }])

  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })

  render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider>
        <MemoryRouter initialEntries={initialEntries}>
          <Downloads />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  )

  await waitFor(() => {
    expect(screen.getByText('Downloads')).toBeInTheDocument()
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Downloads page', () => {
  it('shows only Active and History tabs (Upcoming removed)', async () => {
    await renderDownloads()
    expect(screen.getByRole('tab', { name: /Active/ })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /History/ })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /Upcoming/ })).not.toBeInTheDocument()
  })

  it('falls back to the Active tab for ?tab=upcoming', async () => {
    await renderDownloads({ initialEntries: ['/downloads?tab=upcoming'] })
    expect(screen.getByRole('tab', { name: /Active/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('links to the Scheduled page from the Active tab', async () => {
    await renderDownloads()
    const link = screen.getByRole('link', { name: 'Scheduled' })
    expect(link).toHaveAttribute('href', '/scheduled')
  })

  it('renders an active download as a pipeline card with stage progress', async () => {
    await renderDownloads({
      downloads: [{ ...activeDownload, comskip_progress: 0, transcode_progress: 0 }],
    })
    expect(screen.getByText('Evening News')).toBeInTheDocument()
    expect(screen.getByText('Download')).toBeInTheDocument()
    expect(screen.getByText('Commercials')).toBeInTheDocument()
    expect(screen.getByText('Re-encode')).toBeInTheDocument()
    expect(screen.getByText('42%')).toBeInTheDocument()
    expect(screen.getByText('500.0 MB / 1000.0 MB')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel download' })).toBeInTheDocument()
  })

  it('shows an interrupted recording in history with recorded versus expected duration', async () => {
    await renderDownloads({
      downloads: [interruptedDownload],
      initialEntries: ['/downloads?tab=history'],
    })
    expect(await screen.findByText('Championship Final')).toBeInTheDocument()
    expect(screen.getByText(/Interrupted - 54 min of 60 min/)).toBeInTheDocument()
    expect(screen.getByText(/Connection to the provider was lost/)).toBeInTheDocument()
  })

  it('offers a filter and a retry for interrupted recordings', async () => {
    await renderDownloads({
      downloads: [completedDownload, interruptedDownload],
      initialEntries: ['/downloads?tab=history'],
    })
    await screen.findByText('Championship Final')

    const filter = screen.getByRole('radio', { name: 'Interrupted' })
    fireEvent.click(filter)

    await waitFor(() => {
      expect(screen.queryByText('Nature Documentary')).not.toBeInTheDocument()
    })
    expect(screen.getByText('Championship Final')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('shows compact history rows with a segmented status filter', async () => {
    await renderDownloads({
      downloads: [completedDownload, failedDownload],
      initialEntries: ['/downloads?tab=history'],
    })

    expect(screen.getByText('Nature Documentary')).toBeInTheDocument()
    expect(screen.getByText('Late Show')).toBeInTheDocument()
    expect(screen.getByText('Provider returned 404')).toBeInTheDocument()
    expect(screen.getByText(/nature-doc\.mkv/)).toBeInTheDocument()
    expect(screen.getByText('2.0 GB')).toBeInTheDocument()

    // Completed rows offer play/download-file; failed rows offer retry
    expect(screen.getByRole('link', { name: 'Play' })).toHaveAttribute('href', '/downloads/2/play')
    expect(screen.getByRole('link', { name: 'Download file' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()

    // Filter down to failed only via the segmented control
    fireEvent.click(screen.getByRole('radio', { name: 'Failed' }))
    await waitFor(() => {
      expect(screen.queryByText('Nature Documentary')).not.toBeInTheDocument()
    })
    expect(screen.getByText('Late Show')).toBeInTheDocument()
  })
})
