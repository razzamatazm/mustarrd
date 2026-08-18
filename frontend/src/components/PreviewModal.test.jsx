import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { renderWithMantine } from '../test/renderWithProviders'

const destroy = vi.fn()
const destroyHls = vi.fn()
const attachMpegts = vi.fn(() => destroy)
const attachHls = vi.fn(() => destroyHls)
let tsSupported = true

vi.mock('../utils/playbackEngine', async () => {
  const actual = await vi.importActual('../utils/playbackEngine')
  return {
    ...actual,
    attachMpegts: (...args) => attachMpegts(...args),
    attachHls: (...args) => attachHls(...args),
    mpegtsSupported: () => tsSupported,
  }
})

const PreviewModal = (await import('./PreviewModal')).default

const program = {
  title: 'Wheel of Fortune',
  channel_id: '42',
  start_timestamp: 1700000000,
  stop_timestamp: 1700001800,
}

function render(props = {}) {
  return renderWithMantine(
    <PreviewModal
      opened
      onClose={() => {}}
      program={program}
      channel={{ stream_id: '42' }}
      accountId={1}
      {...props}
    />
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  tsSupported = true
  window.localStorage.clear()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('PreviewModal', () => {
  it('attaches mpegts.js to the portal-rendered video element', async () => {
    render()

    // Regression: the video lives in a Modal portal that mounts after open;
    // attaching must still happen once the element exists.
    await waitFor(() => expect(attachMpegts).toHaveBeenCalledTimes(1))
    const [videoEl, url, opts] = attachMpegts.mock.calls[0]
    expect(videoEl).toBeInstanceOf(HTMLVideoElement)
    expect(url).toContain('/api/accounts/1/channels/42/preview?')
    expect(url).toContain('start_timestamp=1700000000')
    expect(opts).toMatchObject({ live: true })
    expect(attachHls).not.toHaveBeenCalled()
  })

  it('destroys the player when the modal closes', async () => {
    const { rerender } = render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    rerender(
      <MantineProvider>
        <PreviewModal opened={false} onClose={() => {}} program={program} channel={{ stream_id: '42' }} accountId={1} />
      </MantineProvider>
    )
    await waitFor(() => expect(destroy).toHaveBeenCalled())
  })

  it('falls back to the Converted preview when mpegts.js errors', async () => {
    const { findByText } = render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    const { onError } = attachMpegts.mock.calls[0][2]
    act(() => onError('MediaError: codec unsupported', {}))

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))
    expect(attachHls.mock.calls[0][1]).toContain(
      '/api/accounts/1/channels/42/preview/hls/playlist.m3u8?'
    )
    expect(await findByText(/needs converting/i)).toBeTruthy()
  })

  it('remembers the failure so the next open skips the Direct attempt', async () => {
    const first = render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())
    act(() => attachMpegts.mock.calls[0][2].onError('boom', {}))
    await waitFor(() => expect(attachHls).toHaveBeenCalled())
    first.unmount()

    vi.clearAllMocks()
    render()

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))
    expect(attachMpegts).not.toHaveBeenCalled()
  })

  it('goes straight to Converted when the browser has no MSE (iOS Safari)', async () => {
    tsSupported = false
    render()

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))
    expect(attachMpegts).not.toHaveBeenCalled()
  })

  it('converts when the Direct attempt plays nothing within the watchdog window', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    // jsdom leaves readyState at 0: nothing ever decoded.
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })

    await waitFor(() => expect(attachHls).toHaveBeenCalledTimes(1))
  })

  it('leaves a healthy Direct preview alone when the watchdog fires', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    const videoEl = attachMpegts.mock.calls[0][0]
    Object.defineProperty(videoEl, 'readyState', { value: 4, configurable: true })
    Object.defineProperty(videoEl, 'currentTime', { value: 3, configurable: true })

    await act(async () => {
      vi.advanceTimersByTime(6000)
    })

    expect(attachHls).not.toHaveBeenCalled()
  })

  it('does not convert when autoplay is blocked but the stream decoded fine', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    const videoEl = attachMpegts.mock.calls[0][0]
    Object.defineProperty(videoEl, 'readyState', { value: 4, configurable: true })
    Object.defineProperty(videoEl, 'paused', { value: true, configurable: true })

    await act(async () => {
      vi.advanceTimersByTime(6000)
    })

    expect(attachHls).not.toHaveBeenCalled()
  })

  it('reports the shared preview cap instead of converting on a 429', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { findByText } = render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    act(() => attachMpegts.mock.calls[0][2].onError('NetworkError', { code: 429 }))

    expect(await findByText(/Preview limit reached/i)).toBeTruthy()
    // Converting shares the same budget, so the watchdog must not turn a
    // "come back later" into a second refused request.
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })
    expect(attachHls).not.toHaveBeenCalled()
  })

  it('does not convert or remember when the provider is unreachable', async () => {
    // Converting cannot fix a provider that is down, and remembering it would
    // pin a perfectly playable channel to FFmpeg for good.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { findByText } = render()
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    act(() => attachMpegts.mock.calls[0][2].onError('NetworkError: EXCEPTION', { code: 502 }))

    expect(await findByText(/provider could not be reached/i)).toBeTruthy()
    await act(async () => {
      vi.advanceTimersByTime(6000)
    })
    expect(attachHls).not.toHaveBeenCalled()
    expect(window.localStorage.length).toBe(0)
  })

  it('surfaces the cap when the Converted preview itself is refused', async () => {
    tsSupported = false
    const { findByText } = render()
    await waitFor(() => expect(attachHls).toHaveBeenCalled())

    act(() => attachHls.mock.calls[0][2].onError('manifestLoadError', { response: { code: 429 } }))

    expect(await findByText(/Preview limit reached/i)).toBeTruthy()
  })
})
