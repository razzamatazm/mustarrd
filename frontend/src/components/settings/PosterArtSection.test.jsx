import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithMantine } from '../../test/renderWithProviders'
import PosterArtSection from './PosterArtSection'
import { settingsApi } from '../../api'

vi.mock('../../api', () => ({
  settingsApi: { checkPosterArtKey: vi.fn() },
}))

const defaults = {
  poster_art_enabled: true,
  poster_art_save_to_library: true,
  tmdb_api_key_set: false,
}

function renderSection(overrides = {}) {
  const onChange = vi.fn()
  renderWithMantine(<PosterArtSection formData={{ ...defaults, ...overrides }} onChange={onChange} />)
  return { onChange }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('PosterArtSection', () => {
  it('says what is sent, to whom, and that a recording is never held up', () => {
    renderSection()

    expect(screen.getByText(/sends the name of the\s+show/)).toBeInTheDocument()
    expect(screen.getByText(/Nothing about you or your provider is sent/)).toBeInTheDocument()
    expect(screen.getByText(/can never delay or fail a recording/)).toBeInTheDocument()
  })

  it('has both switches on by default', () => {
    renderSection()

    expect(screen.getByLabelText('Fetch poster art')).toBeChecked()
    expect(screen.getByLabelText('Save a copy next to the recording')).toBeChecked()
  })

  it('reports each switch through onChange', () => {
    const { onChange } = renderSection()

    fireEvent.click(screen.getByLabelText('Fetch poster art'))
    fireEvent.click(screen.getByLabelText('Save a copy next to the recording'))

    expect(onChange).toHaveBeenCalledWith('poster_art_enabled', false)
    expect(onChange).toHaveBeenCalledWith('poster_art_save_to_library', false)
  })

  it('turns everything else off when poster art is off', () => {
    renderSection({ poster_art_enabled: false })

    expect(screen.getByLabelText('Save a copy next to the recording')).toBeDisabled()
    expect(screen.getByLabelText('Save a copy next to the recording')).not.toBeChecked()
    expect(screen.getByLabelText('TMDB API key')).toBeDisabled()
  })

  it('never shows a saved key, only that there is one', () => {
    renderSection({ tmdb_api_key_set: true })

    expect(screen.getByLabelText('TMDB API key')).toHaveValue('')
    expect(screen.getByText(/A key is saved/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove saved key' })).toBeInTheDocument()
  })

  it('reports a pasted key', () => {
    const { onChange } = renderSection()

    fireEvent.change(screen.getByLabelText('TMDB API key'), { target: { value: 'abc123' } })

    expect(onChange).toHaveBeenCalledWith('tmdb_api_key', 'abc123')
  })

  it('treats typing a key and deleting it again as no change, not as removal', () => {
    const { onChange } = renderSection({ tmdb_api_key_set: true, tmdb_api_key: 'abc' })

    fireEvent.change(screen.getByLabelText('TMDB API key'), { target: { value: '' } })

    expect(onChange).toHaveBeenLastCalledWith('tmdb_api_key', null)
  })

  it('removes the saved key only when asked to', () => {
    const { onChange } = renderSection({ tmdb_api_key_set: true })

    fireEvent.click(screen.getByRole('button', { name: 'Remove saved key' }))

    expect(onChange).toHaveBeenCalledWith('tmdb_api_key', '')
  })

  it('lets you change your mind about removing it', () => {
    const { onChange } = renderSection({ tmdb_api_key_set: true, tmdb_api_key: '' })

    expect(screen.getByText(/will be removed when you save/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Keep saved key' }))

    expect(onChange).toHaveBeenCalledWith('tmdb_api_key', null)
  })

  it('checks the key as typed and shows what TMDB said', async () => {
    settingsApi.checkPosterArtKey.mockResolvedValue({ ok: true, message: 'TMDB accepted this key.' })
    renderSection({ tmdb_api_key: 'abc123' })

    fireEvent.click(screen.getByRole('button', { name: 'Check key' }))

    await waitFor(() => expect(settingsApi.checkPosterArtKey).toHaveBeenCalledWith('abc123'))
    expect(await screen.findByText('TMDB accepted this key.')).toBeInTheDocument()
  })

  it('checks the saved key when nothing is typed', async () => {
    settingsApi.checkPosterArtKey.mockResolvedValue({ ok: false, message: 'TMDB did not accept this key.' })
    renderSection({ tmdb_api_key_set: true })

    fireEvent.click(screen.getByRole('button', { name: 'Check key' }))

    await waitFor(() => expect(settingsApi.checkPosterArtKey).toHaveBeenCalledWith(undefined))
    expect(await screen.findByText('TMDB did not accept this key.')).toBeInTheDocument()
  })

  it('has nothing to check until there is a key', () => {
    renderSection()

    expect(screen.getByRole('button', { name: 'Check key' })).toBeDisabled()
  })
})
