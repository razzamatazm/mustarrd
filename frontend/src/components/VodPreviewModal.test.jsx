import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithQuery } from '../test/renderWithProviders'

const destroyHls = vi.fn()
const attachHls = vi.fn(() => destroyHls)

vi.mock('../utils/playbackEngine', async () => {
  const actual = await vi.importActual('../utils/playbackEngine')
  return { ...actual, attachHls: (...args) => attachHls(...args) }
})

const previewDuration = vi.fn()

vi.mock('../api', async () => {
  const actual = await vi.importActual('../api')
  return {
    ...actual,
    vodApi: {
      ...actual.vodApi,
      previewDuration: (...args) => previewDuration(...args),
    },
  }
})

const VodPreviewModal = (await import('./VodPreviewModal')).default

function render(props = {}) {
  return renderWithQuery(
    <VodPreviewModal
      opened
      onClose={() => {}}
      accountId={1}
      kind="movie"
      itemId="42"
      containerExtension="mkv"
      title="The Thing"
      {...props}
    />
  )
}

function lastPlaylistUrl() {
  return attachHls.mock.calls.at(-1)[1]
}

beforeEach(() => {
  vi.clearAllMocks()
  previewDuration.mockResolvedValue({ duration: 7200 })
})

describe('VodPreviewModal', () => {
  it('always converts: no native src attempt for any container', async () => {
    // The provider ships no browser-playable VOD — MKV, or MP4 with AC-3 —
    // so a Direct attempt would only ever be a silent or blank player.
    render({ containerExtension: 'mp4' })

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))
    const video = document.querySelector('video')
    expect(video.getAttribute('src')).toBeNull()
    expect(lastPlaylistUrl()).toContain('/hls/playlist.m3u8')
  })

  it('builds the movie playlist URL with the container extension', async () => {
    render()
    await waitFor(() => expect(attachHls).toHaveBeenCalled())
    expect(lastPlaylistUrl()).toBe(
      '/api/vod/preview/1/movie/42/hls/playlist.m3u8?container_extension=mkv'
    )
  })

  it('builds the episode playlist URL from the series path', async () => {
    render({ kind: 'episode', itemId: '907', seriesId: '88', containerExtension: 'mp4' })
    await waitFor(() => expect(attachHls).toHaveBeenCalled())
    expect(lastPlaylistUrl()).toBe(
      '/api/vod/preview/1/episode/907/hls/playlist.m3u8?container_extension=mp4'
    )
    expect(previewDuration).toHaveBeenCalledWith(1, 'episode', '907', {
      seriesId: '88',
      containerExtension: 'mp4',
    })
  })

  it('shows the full length of the film before FFmpeg has produced it', async () => {
    render()
    // 7200s of film, none of it converted yet: the scrub bar is still 2 hours.
    const slider = await screen.findByRole('slider', { name: 'Seek' })
    expect(slider).toHaveAttribute('aria-valuemax', '7200')
    expect(screen.getByText('0:00 / 2:00:00')).toBeInTheDocument()
  })

  it('restarts the session at the requested offset when scrubbing ahead', async () => {
    render()
    const slider = await screen.findByRole('slider', { name: 'Seek' })
    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))

    // Nothing is buffered, so any target is outside the produced range and
    // has to restart FFmpeg rather than seek the element.
    fireEvent.keyDown(slider, { key: 'ArrowRight' })

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(2))
    expect(lastPlaylistUrl()).toContain('start=1.000')
  })

  it('falls back to the browser controls when the length is unknown', async () => {
    previewDuration.mockResolvedValue({ duration: null })
    render()
    await waitFor(() => expect(attachHls).toHaveBeenCalled())
    expect(screen.queryByTestId('transport-bar')).not.toBeInTheDocument()
    expect(document.querySelector('video')).toHaveAttribute('controls')
  })

  it('explains a refused preview rather than sitting blank', async () => {
    render()
    await waitFor(() => expect(attachHls).toHaveBeenCalled())

    const { onError } = attachHls.mock.calls.at(-1)[2]
    onError('manifestLoadError', { response: { code: 429 } })

    expect(await screen.findByText(/Preview limit reached/)).toBeInTheDocument()
  })

  it('reports a failed conversion without blaming downloads', async () => {
    render()
    await waitFor(() => expect(attachHls).toHaveBeenCalled())

    const { onError } = attachHls.mock.calls.at(-1)[2]
    onError('manifestLoadError', { response: { code: 502 } })

    expect(await screen.findByText(/Downloading still works normally/)).toBeInTheDocument()
  })

  it('tears the player down when it closes', async () => {
    const { rerender } = render()
    await waitFor(() => expect(attachHls).toHaveBeenCalled())

    rerender(<div />)
    expect(destroyHls).toHaveBeenCalled()
  })
})
