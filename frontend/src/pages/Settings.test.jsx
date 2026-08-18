import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import Settings from './Settings'
import {
  accountsApi,
  adminPlexApi,
  adminUsersApi,
  authApi,
  downloadsApi,
  epgApi,
  settingsApi,
} from '../api'

vi.mock('../api', () => ({
  authApi: { status: vi.fn() },
  settingsApi: {
    get: vi.fn(),
    update: vi.fn(),
    getTemplates: vi.fn(),
    getTools: vi.fn(),
    getFoldersStatus: vi.fn(),
  },
  epgApi: { status: vi.fn(), refresh: vi.fn() },
  accountsApi: { list: vi.fn() },
  adminUsersApi: { list: vi.fn(), create: vi.fn(), delete: vi.fn(), update: vi.fn() },
  adminPlexApi: {
    get: vi.fn(),
    save: vi.fn(),
    disconnect: vi.fn(),
    listResources: vi.fn(),
    listLibraries: vi.fn(),
    connectStart: vi.fn(),
    connectComplete: vi.fn(),
  },
  downloadsApi: { diskSpace: vi.fn() },
  logsApi: { list: vi.fn() },
  createLogsWebSocket: vi.fn(() => ({ close: vi.fn() })),
}))

window.scrollTo = window.scrollTo || (() => {})

const baseSettings = {
  download_folder: '/downloads',
  completed_folder: '/completed',
  max_concurrent_downloads: 2,
  min_free_space_gb: 25,
  default_pre_padding_minutes: 1,
  default_post_padding_minutes: 2,
  transcode_enabled: true,
  remux_only: true,
  transcode_format: 'mkv',
  comskip_enabled: false,
  hw_accel: 'cpu',
  max_concurrent_post_processing: 1,
  delete_original_after_transcode: true,
  comskip_path: null,
  comskip_custom_ini_path: null,
  comskip_detect_method: 107,
  comskip_max_commercialbreak: 600,
  comskip_min_commercialbreak: 25,
  comskip_max_commercial_size: 125,
  comskip_min_commercial_size: 4,
  comskip_always_keep_first_seconds: 0,
  comskip_always_keep_last_seconds: 60,
  comskip_remove_before: 0,
  comskip_remove_after: 0,
  comskip_thread_count: 1,
  tv_template: '{show} - S{season:02d}E{episode:02d} - {title}',
  movie_template: '{title} ({year})',
  sports_template: '{title} - {date}',
  default_template: '{title} - {date}',
  default_account_id: null,
}

function renderSettings() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider>
        <MemoryRouter initialEntries={['/settings']}>
          <Settings />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  authApi.status.mockResolvedValue({ authenticated: true, is_admin: true, provider: 'local' })
  settingsApi.get.mockResolvedValue({ ...baseSettings })
  settingsApi.getTemplates.mockResolvedValue({
    tv_show: { variables: [{ name: 'show', description: 'Show' }], example: '', rendered_example: '' },
    movie: { variables: [{ name: 'title', description: 'Title' }], example: '', rendered_example: '' },
    sports: { variables: [{ name: 'title', description: 'Title' }], example: '', rendered_example: '' },
    default: { variables: [{ name: 'title', description: 'Title' }], example: '', rendered_example: '' },
  })
  settingsApi.getTools.mockResolvedValue({
    ffmpeg: { available: true },
    comskip: { available: true },
    hardware_accels: [{ id: 'cpu', name: 'CPU (Software)', available: true }],
    vaapi: { enabled: false },
  })
  settingsApi.getFoldersStatus.mockResolvedValue({
    download_folder: { path: '/downloads', exists: true, writable: true, error: null },
    completed_folder: { path: '/completed', exists: true, writable: true, error: null },
  })
  settingsApi.update.mockImplementation(async (patch) => ({ ...baseSettings, ...patch }))
  epgApi.status.mockResolvedValue({ running: false, last_completed_at: null, interval_hours: 8 })
  accountsApi.list.mockResolvedValue([])
  adminUsersApi.list.mockResolvedValue([])
  adminPlexApi.get.mockResolvedValue({ token_configured: false })
  downloadsApi.diskSpace.mockResolvedValue({ available: true, disk_free_gb: 412, is_low: false })
})

describe('Settings page', () => {
  it('renders the grouped rail with renamed sections and lands on Recording', async () => {
    renderSettings()

    expect(await screen.findByText('Connections')).toBeInTheDocument()
    expect(screen.getByText('System')).toBeInTheDocument()
    expect(screen.getByText('Commercial Skip')).toBeInTheDocument()
    expect(screen.getByText('Program Guide')).toBeInTheDocument()

    // Default admin landing section is Recording.
    expect(screen.getByText('Max concurrent downloads')).toBeInTheDocument()
    expect(screen.getAllByText('Writable').length).toBe(2)
    expect(screen.getByText('412 GB free on recording drive')).toBeInTheDocument()
  })

  it('navigates via settings search', async () => {
    renderSettings()
    await screen.findByText('Connections')

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'commercials' } })
    fireEvent.click(await screen.findByText('Commercials'))

    expect(await screen.findByText('Container')).toBeInTheDocument()
    expect(screen.getByText('Conversion method')).toBeInTheDocument()
  })

  it('shows the save bar with a change count and saves only changed fields', async () => {
    renderSettings()
    await screen.findByText('Max concurrent downloads')

    fireEvent.click(screen.getByRole('button', { name: 'Increase Max concurrent downloads' }))

    expect(await screen.findByText('1')).toBeInTheDocument()
    expect(screen.getByText(/unsaved change$/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))

    await waitFor(() => {
      expect(settingsApi.update).toHaveBeenCalled()
    })
    expect(settingsApi.update.mock.calls[0][0]).toEqual({ max_concurrent_downloads: 3 })
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Save Changes' })).not.toBeInTheDocument()
    })
  })

  it('discards edits from the save bar', async () => {
    renderSettings()
    await screen.findByText('Max concurrent downloads')

    fireEvent.click(screen.getByRole('button', { name: 'Increase Max concurrent downloads' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard' }))

    expect(screen.queryByRole('button', { name: 'Save Changes' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Max concurrent downloads' })).toHaveValue('2')
  })

  it('offers a GPU device picker listing every detected render node', async () => {
    settingsApi.get.mockResolvedValue({
      ...baseSettings,
      remux_only: false,
      hw_accel: 'vaapi',
      vaapi_render_device: '',
    })
    settingsApi.getTools.mockResolvedValue({
      ffmpeg: { available: true },
      comskip: { available: true },
      hardware_accels: [
        { id: 'cpu', name: 'CPU (Software)', available: true },
        { id: 'vaapi', name: 'VA-API', available: true },
      ],
      vaapi: {
        enabled: true,
        source: 'auto-detected',
        device: '/dev/dri/renderD128',
        device_locked_by_env: false,
        available_devices: [
          { device: '/dev/dri/renderD128', kernel_driver: 'i915', vendor: '0x8086' },
          { device: '/dev/dri/renderD129', kernel_driver: 'amdgpu', vendor: '0x1002' },
        ],
      },
    })

    renderSettings()
    await screen.findByText('Connections')
    fireEvent.click(screen.getByText('Post-Processing'))

    const picker = await screen.findByRole('textbox', { name: 'GPU device' })
    fireEvent.click(picker)

    expect(await screen.findByText('renderD129 — AMD')).toBeInTheDocument()
    expect(screen.getByText('renderD128 — Intel')).toBeInTheDocument()

    fireEvent.click(screen.getByText('renderD129 — AMD'))
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
    await waitFor(() => {
      expect(settingsApi.update).toHaveBeenCalled()
    })
    expect(settingsApi.update.mock.calls[0][0]).toEqual({
      vaapi_render_device: '/dev/dri/renderD129',
    })
  })

  it('disables the GPU picker when the env var pins the device', async () => {
    settingsApi.get.mockResolvedValue({
      ...baseSettings,
      remux_only: false,
      hw_accel: 'vaapi',
      vaapi_render_device: '',
    })
    settingsApi.getTools.mockResolvedValue({
      ffmpeg: { available: true },
      comskip: { available: true },
      hardware_accels: [
        { id: 'cpu', name: 'CPU (Software)', available: true },
        { id: 'vaapi', name: 'VA-API', available: true },
      ],
      vaapi: {
        enabled: true,
        source: 'auto-detected',
        device: '/dev/dri/renderD129',
        device_locked_by_env: true,
        available_devices: [
          { device: '/dev/dri/renderD128', kernel_driver: 'i915', vendor: '0x8086' },
        ],
      },
    })

    renderSettings()
    await screen.findByText('Connections')
    fireEvent.click(screen.getByText('Post-Processing'))

    expect(
      await screen.findByRole('textbox', { name: 'GPU device' }),
    ).toBeDisabled()
    expect(
      screen.getByText(/Pinned by CATCHUP_VAAPI_RENDER_DEVICE/),
    ).toBeInTheDocument()
  })

  it('maps the two-step format picker onto the transcode fields', async () => {
    renderSettings()
    await screen.findByText('Connections')

    fireEvent.click(screen.getByText('Post-Processing'))

    // remux + mkv + no comskip from the fixture
    expect(await screen.findByRole('radio', { name: 'MKV' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Remux (fast)' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Off' })).toBeChecked()
  })

  it('offers Off / Mark only / Cut out and Mark does not force a re-encode', async () => {
    renderSettings()
    await screen.findByText('Connections')

    fireEvent.click(screen.getByText('Post-Processing'))

    // fixture has comskip off → Off selected
    expect(await screen.findByRole('radio', { name: 'Off' })).toBeChecked()

    // Mark only: detect but do not cut; must NOT force transcode fields
    fireEvent.click(screen.getByRole('radio', { name: 'Mark only' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
    await waitFor(() => {
      expect(settingsApi.update).toHaveBeenCalled()
    })
    expect(settingsApi.update.mock.calls[0][0]).toEqual({
      comskip_enabled: true,
      comskip_cut: false,
    })
  })

  it('Cut out enables comskip and cuts (forces a container conversion)', async () => {
    renderSettings()
    await screen.findByText('Connections')

    fireEvent.click(screen.getByText('Post-Processing'))

    fireEvent.click(await screen.findByRole('radio', { name: 'Cut out' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
    await waitFor(() => {
      expect(settingsApi.update).toHaveBeenCalled()
    })
    // fixture is already MKV/transcode-enabled, so only the comskip fields change
    expect(settingsApi.update.mock.calls[0][0]).toEqual({
      comskip_enabled: true,
      comskip_cut: true,
    })
  })

  it('allows Mark only on Keep .ts but disables Cut out, with a chapter warning', async () => {
    renderSettings()
    await screen.findByText('Connections')

    fireEvent.click(screen.getByText('Post-Processing'))
    fireEvent.click(await screen.findByRole('radio', { name: 'Keep .ts' }))

    expect(screen.getByRole('radio', { name: 'Cut out' })).toBeDisabled()
    expect(screen.getByRole('radio', { name: 'Mark only' })).not.toBeDisabled()

    fireEvent.click(screen.getByRole('radio', { name: 'Mark only' }))
    expect(screen.getByText(/Plex chapter-skip needs MKV or MP4/i)).toBeInTheDocument()
    // method row hidden for .ts
    expect(screen.queryByText('Conversion method')).not.toBeInTheDocument()
  })
})
