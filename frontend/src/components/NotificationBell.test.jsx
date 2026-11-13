import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import NotificationBell from './NotificationBell'
import { notificationsApi } from '../api'

vi.mock('../api', () => ({
  notificationsApi: {
    list: vi.fn(),
    dismiss: vi.fn(),
    dismissAll: vi.fn(),
  },
}))

const expiring = {
  id: 1,
  kind: 'account_expiring',
  severity: 'warning',
  title: 'Account expiring soon',
  message: 'Trex expires in 3 days, on Nov 15, 2026.',
  link: '/settings?section=accounts',
  created_at: '2026-11-12T10:00:00Z',
}

const failed = {
  id: 2,
  kind: 'recording_failed',
  severity: 'error',
  title: 'Recording failed',
  message: 'The News on BBC One: Provider refused the catchup request',
  link: '/downloads',
  created_at: '2026-11-12T11:00:00Z',
}

function renderBell() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider>
        <MemoryRouter initialEntries={['/browse']}>
          <Routes>
            <Route path="/browse" element={<NotificationBell />} />
            <Route path="/downloads" element={<div>Downloads page</div>} />
          </Routes>
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>
  )
}

describe('NotificationBell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows how many notifications are waiting', async () => {
    notificationsApi.list.mockResolvedValue({ items: [expiring, failed], count: 2 })
    renderBell()
    expect(await screen.findByRole('button', { name: 'Notifications (2)' })).toBeInTheDocument()
  })

  it('says so when nothing needs attention', async () => {
    notificationsApi.list.mockResolvedValue({ items: [], count: 0 })
    renderBell()
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications' }))
    expect(await screen.findByText('Nothing needs your attention.')).toBeInTheDocument()
  })

  it('lists notifications and dismisses one', async () => {
    notificationsApi.list.mockResolvedValue({ items: [expiring, failed], count: 2 })
    notificationsApi.dismiss.mockResolvedValue({ dismissed: true })
    renderBell()
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications (2)' }))

    expect(await screen.findByText(expiring.message)).toBeInTheDocument()
    expect(screen.getByText(failed.message)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss Recording failed' }))
    await waitFor(() => expect(notificationsApi.dismiss).toHaveBeenCalled())
    expect(notificationsApi.dismiss.mock.calls[0][0]).toBe(2)
    await waitFor(() => expect(notificationsApi.list).toHaveBeenCalledTimes(2))
  })

  it('dismisses everything at once', async () => {
    notificationsApi.list.mockResolvedValue({ items: [expiring, failed], count: 2 })
    notificationsApi.dismissAll.mockResolvedValue({ dismissed: 2 })
    renderBell()
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications (2)' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss all' }))
    await waitFor(() => expect(notificationsApi.dismissAll).toHaveBeenCalled())
  })

  it('opens the page a notification points at', async () => {
    notificationsApi.list.mockResolvedValue({ items: [failed], count: 1 })
    renderBell()
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications (1)' }))
    fireEvent.click(await screen.findByText(failed.message))
    expect(await screen.findByText('Downloads page')).toBeInTheDocument()
  })

  it('opens a link to another site in a new tab', async () => {
    const update = {
      id: 3,
      kind: 'update_available',
      severity: 'warning',
      title: 'Update available',
      message: 'A newer version of Mustarrd is available (3 changes since this build).',
      link: 'https://github.com/razzamatazm/mustarrd/compare/abc1234...main',
      created_at: '2026-11-12T11:00:00Z',
    }
    notificationsApi.list.mockResolvedValue({ items: [update], count: 1 })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    renderBell()
    fireEvent.click(await screen.findByRole('button', { name: 'Notifications (1)' }))
    fireEvent.click(await screen.findByText(update.message))
    expect(open).toHaveBeenCalledWith(update.link, '_blank', 'noopener,noreferrer')
    open.mockRestore()
  })
})
