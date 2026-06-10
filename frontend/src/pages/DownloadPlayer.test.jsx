import { describe, expect, it, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { renderWithQuery } from '../test/renderWithProviders'

const getDownload = vi.fn()
vi.mock('../api', () => ({
  downloadsApi: { get: (...args) => getDownload(...args) },
}))

const attachMpegts = vi.fn(() => () => {})
const attachHls = vi.fn(() => () => {})
vi.mock('../utils/playbackEngine', async () => {
  const actual = await vi.importActual('../utils/playbackEngine')
  return {
    ...actual,
    pickEngine: (path) => actual.pickEngine(path, { tsSupported: true }),
    attachMpegts: (...args) => attachMpegts(...args),
    attachHls: (...args) => attachHls(...args),
  }
})

const DownloadPlayer = (await import('./DownloadPlayer')).default

function renderPlayer(downloadId) {
  return renderWithQuery(
    <MemoryRouter initialEntries={[`/downloads/${downloadId}/play`]}>
      <Routes>
        <Route path="/downloads/:downloadId/play" element={<DownloadPlayer />} />
      </Routes>
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('DownloadPlayer', () => {
  it('shows an error for an invalid id', () => {
    renderPlayer('banana')
    expect(screen.getByText('Invalid download id.')).toBeInTheDocument()
    expect(getDownload).not.toHaveBeenCalled()
  })

  it('plays MKV recordings through the HLS engine', async () => {
    getDownload.mockResolvedValue({
      id: 7,
      output_path: '/data/completed/Show.mkv',
      program_title: 'Show',
    })
    renderPlayer(7)

    await waitFor(() => expect(attachHls).toHaveBeenCalled())
    expect(attachHls.mock.calls[0][1]).toBe('/api/downloads/7/hls/playlist.m3u8')
    expect(attachMpegts).not.toHaveBeenCalled()
  })

  it('plays TS recordings through mpegts.js with the direct file URL', async () => {
    getDownload.mockResolvedValue({
      id: 9,
      output_path: '/data/completed/Game.ts',
      program_title: 'Game',
    })
    renderPlayer(9)

    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())
    expect(attachMpegts.mock.calls[0][1]).toBe('/api/downloads/9/file?action=play')
    expect(attachMpegts.mock.calls[0][2]).toMatchObject({ live: false })
  })

  it('restarts an expired HLS session once, resuming from the same position', async () => {
    getDownload.mockResolvedValue({
      id: 7,
      output_path: '/data/completed/Show.mkv',
      program_title: 'Show',
    })
    renderPlayer(7)

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))
    const video = document.querySelector('video')
    Object.defineProperty(video, 'currentTime', { value: 42.5, writable: true })
    attachHls.mock.calls[0][2].onError('bufferStalledError')

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(2))
    expect(attachHls.mock.calls[1][2]).toMatchObject({ startPosition: 42.5 })
    expect(screen.queryByText(/Playback failed/)).not.toBeInTheDocument()

    // A second fatal error surfaces the failure instead of looping.
    attachHls.mock.calls[1][2].onError('fatalError')
    await waitFor(() => expect(screen.getByText(/Playback failed/)).toBeInTheDocument())
  })

  it('falls back to HLS when mpegts.js reports an error', async () => {
    getDownload.mockResolvedValue({
      id: 9,
      output_path: '/data/completed/Game.ts',
      program_title: 'Game',
    })
    renderPlayer(9)

    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())
    const { onError } = attachMpegts.mock.calls[0][2]
    onError('MediaError: unsupported')

    await waitFor(() => expect(attachHls).toHaveBeenCalled())
    expect(attachHls.mock.calls[0][1]).toBe('/api/downloads/9/hls/playlist.m3u8')
  })
})
