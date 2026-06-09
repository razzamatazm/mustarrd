import { describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithQuery } from '../../test/renderWithProviders'
import FoldersStatus from './FoldersStatus'
import { settingsApi } from '../../api'

vi.mock('../../api', () => ({
  settingsApi: {
    getFoldersStatus: vi.fn(),
  },
}))

const okFolder = (path) => ({ path, exists: true, writable: true, error: null })

describe('FoldersStatus', () => {
  it('shows both folders as writable', async () => {
    settingsApi.getFoldersStatus.mockResolvedValue({
      download_folder: okFolder('/downloads'),
      completed_folder: okFolder('/completed'),
    })

    renderWithQuery(<FoldersStatus />)

    expect(await screen.findByText('Download folder (in progress)')).toBeInTheDocument()
    expect(screen.getByText('Completed recordings folder')).toBeInTheDocument()
    expect(screen.getAllByText('Writable')).toHaveLength(2)
    expect(screen.getByText('/downloads')).toBeInTheDocument()
    expect(screen.getByText('/completed')).toBeInTheDocument()
  })

  it('flags a missing folder and surfaces its error', async () => {
    settingsApi.getFoldersStatus.mockResolvedValue({
      download_folder: okFolder('/downloads'),
      completed_folder: {
        path: '/mnt/nas/recordings',
        exists: false,
        writable: false,
        error: 'Mount point not found',
      },
    })

    renderWithQuery(<FoldersStatus />)

    expect(await screen.findByText('Missing')).toBeInTheDocument()
    expect(screen.getByText('Mount point not found')).toBeInTheDocument()
    expect(screen.getByText('Writable')).toBeInTheDocument()
  })

  it('flags an existing but unwritable folder', async () => {
    settingsApi.getFoldersStatus.mockResolvedValue({
      download_folder: {
        path: '/downloads',
        exists: true,
        writable: false,
        error: 'Permission denied',
      },
      completed_folder: okFolder('/completed'),
    })

    renderWithQuery(<FoldersStatus />)

    expect(await screen.findByText('Not writable')).toBeInTheDocument()
    expect(screen.getByText('Permission denied')).toBeInTheDocument()
  })

  it('shows an alert when the status request fails', async () => {
    settingsApi.getFoldersStatus.mockRejectedValue(new Error('Network down'))

    renderWithQuery(<FoldersStatus />)

    expect(
      await screen.findByText(/Could not check folder status: Network down/)
    ).toBeInTheDocument()
  })
})
