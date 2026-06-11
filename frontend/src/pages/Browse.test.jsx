import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, screen } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import Browse from './Browse'
import classes from './Browse.module.css'

vi.mock('../api', () => ({
  authApi: {
    status: vi.fn().mockResolvedValue({ authenticated: true, is_admin: true }),
    getPreferences: vi.fn().mockResolvedValue({ show_future_programs: false }),
    updatePreferences: vi.fn(),
  },
  accountsApi: {
    publicList: vi.fn().mockResolvedValue([{ id: 1, name: 'Main', guide_offset_hours: 0 }]),
  },
  channelsApi: {
    getChannels: vi
      .fn()
      .mockResolvedValue([{ stream_id: 1, name: 'Channel One', starred: false, tv_archive: 1 }]),
    getChannel: vi.fn(),
    getEpg: vi.fn().mockResolvedValue([]),
    toggleStar: vi.fn(),
  },
  epgApi: { search: vi.fn().mockResolvedValue([]) },
  settingsApi: { getPublic: vi.fn().mockResolvedValue({ show_future_programs: false }) },
  downloadsApi: { list: vi.fn().mockResolvedValue([]) },
  vodApi: {
    getMovieCategories: vi.fn().mockResolvedValue([]),
    getSeriesCategories: vi.fn().mockResolvedValue([]),
    getMovies: vi.fn().mockResolvedValue([]),
    getSeries: vi.fn().mockResolvedValue([]),
  },
}))

function renderBrowse() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider>
        <MemoryRouter initialEntries={['/browse']}>
          <Browse />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  )
}

describe('Browse panel viewport sizing', () => {
  // Regression for the shrunk-window scroll bug: panel heights were pinned to
  // a hard-coded viewport offset, so when the chrome above them grew (low-disk
  // banner, wrapped header) the channel rail poked below the fold and a
  // near-useless page scrollbar appeared next to the rail's own scrollbar.
  // Browse must measure the panel's real top offset into --panel-top instead.
  // jsdom has no layout, so the geometry itself is verified by stubbing the
  // panel's rect; the real overflow behavior can only be seen in a browser.
  it('publishes the measured panel top as --panel-top on the page root', async () => {
    const { container } = renderBrowse()
    await screen.findByText('Channel One')

    const page = container.querySelector(`.${classes.page}`)
    const panel = container.querySelector(`.${classes.panel}`)
    expect(page).not.toBeNull()
    expect(panel).not.toBeNull()

    // jsdom: every element has offsetParent null and zero-size rects
    Object.defineProperty(panel, 'offsetParent', { get: () => document.body })
    panel.getBoundingClientRect = () => ({ top: 218, bottom: 0, left: 0, right: 0, width: 0, height: 0 })

    fireEvent(window, new Event('resize'))

    await waitFor(() => {
      expect(page.style.getPropertyValue('--panel-top')).toBe('218px')
    })
  })

  it('re-measures when the window is resized', async () => {
    const { container } = renderBrowse()
    await screen.findByText('Channel One')

    const page = container.querySelector(`.${classes.page}`)
    const panel = container.querySelector(`.${classes.panel}`)
    Object.defineProperty(panel, 'offsetParent', { get: () => document.body })

    let top = 218
    panel.getBoundingClientRect = () => ({ top, bottom: 0, left: 0, right: 0, width: 0, height: 0 })
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(page.style.getPropertyValue('--panel-top')).toBe('218px'))

    // banner appears / header wraps → panel sits lower
    top = 288
    fireEvent(window, new Event('resize'))
    await waitFor(() => expect(page.style.getPropertyValue('--panel-top')).toBe('288px'))
  })
})
