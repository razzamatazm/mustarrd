import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'

import { renderWithQuery } from '../../test/renderWithProviders'
import { accountsApi, epgApi, settingsApi } from '../../api'
import AccountsSection, { catchupStyleLabel } from './AccountsSection'

vi.mock('../../api', () => ({
  accountsApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    delete: vi.fn(),
    test: vi.fn(),
  },
  epgApi: { status: vi.fn(), refresh: vi.fn() },
  settingsApi: { get: vi.fn(), update: vi.fn() },
}))

const baseAccount = {
  id: 1,
  name: 'Provider One',
  server_url: 'https://provider.example.com',
  username: 'user',
  is_active: true,
  guide_offset_hours: 0,
  catchup_url_style: 'auto',
  catchup_url_style_resolved: null,
}

function mockAccounts(overrides = {}) {
  accountsApi.list.mockResolvedValue([{ ...baseAccount, ...overrides }])
  settingsApi.get.mockResolvedValue({ default_account_id: 1 })
  epgApi.status.mockResolvedValue({ running: false })
}

describe('catchupStyleLabel', () => {
  it('reports the probed query style while on automatic', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'auto', catchup_url_style_resolved: 'query' })
    ).toBe('Automatic — using query style')
  })

  it('reports the probed path style while on automatic', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'auto', catchup_url_style_resolved: 'path' })
    ).toBe('Automatic — using path style')
  })

  it('says the style is undetermined when nothing has been probed yet', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'auto', catchup_url_style_resolved: null })
    ).toBe('Automatic — style not yet determined')
  })

  it('reports a forced path style', () => {
    expect(
      catchupStyleLabel({ catchup_url_style: 'path', catchup_url_style_resolved: 'query' })
    ).toBe('Catchup URL: path style')
  })

  it('reports a forced query style', () => {
    expect(catchupStyleLabel({ catchup_url_style: 'query' })).toBe('Catchup URL: query style')
  })

  it('falls back to automatic when the account has no setting', () => {
    expect(catchupStyleLabel({})).toBe('Automatic — style not yet determined')
    expect(catchupStyleLabel(null)).toBe('Automatic — style not yet determined')
  })
})

describe('AccountsSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows the resolved catchup style on the account card', async () => {
    mockAccounts({ catchup_url_style_resolved: 'query' })

    renderWithQuery(<AccountsSection />)

    expect(await screen.findByText('Automatic — using query style')).toBeInTheDocument()
  })

  it('shows the forced catchup style on the account card', async () => {
    mockAccounts({ catchup_url_style: 'query', catchup_url_style_resolved: null })

    renderWithQuery(<AccountsSection />)

    expect(await screen.findByText('Catchup URL: query style')).toBeInTheDocument()
  })

  it('round-trips the catchup URL style select into the update payload', async () => {
    mockAccounts({ catchup_url_style: 'auto' })
    accountsApi.update.mockResolvedValue({})

    renderWithQuery(<AccountsSection />)

    fireEvent.click(await screen.findByRole('button', { name: /Edit/ }))

    const select = await screen.findByRole('textbox', { name: 'Catchup URL style' })
    expect(select).toHaveValue('Automatic')

    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'Query (/streaming/timeshift.php)' }))
    expect(select).toHaveValue('Query (/streaming/timeshift.php)')

    fireEvent.click(screen.getByRole('button', { name: 'Update' }))

    await waitFor(() => {
      expect(accountsApi.update).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ catchup_url_style: 'query' })
      )
    })
  })

  it('defaults new accounts to automatic', async () => {
    mockAccounts()
    accountsApi.create.mockResolvedValue({})

    renderWithQuery(<AccountsSection />)

    fireEvent.click(await screen.findByRole('button', { name: 'Add Account' }))

    const select = await screen.findByRole('textbox', { name: 'Catchup URL style' })
    expect(select).toHaveValue('Automatic')
  })
})
