import { describe, expect, it, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { renderWithMantine } from '../test/renderWithProviders'

const destroy = vi.fn()
const attachMpegts = vi.fn(() => destroy)
vi.mock('../utils/playbackEngine', () => ({
  attachMpegts: (...args) => attachMpegts(...args),
  mpegtsSupported: () => true,
}))

const PreviewModal = (await import('./PreviewModal')).default

const program = {
  title: 'Wheel of Fortune',
  channel_id: '42',
  start_timestamp: 1700000000,
  stop_timestamp: 1700001800,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('PreviewModal', () => {
  it('attaches mpegts.js to the portal-rendered video element', async () => {
    renderWithMantine(
      <PreviewModal opened onClose={() => {}} program={program} channel={{ stream_id: '42' }} accountId={1} />
    )

    // Regression: the video lives in a Modal portal that mounts after open;
    // attaching must still happen once the element exists.
    await waitFor(() => expect(attachMpegts).toHaveBeenCalledTimes(1))
    const [videoEl, url, opts] = attachMpegts.mock.calls[0]
    expect(videoEl).toBeInstanceOf(HTMLVideoElement)
    expect(url).toContain('/api/accounts/1/channels/42/preview?')
    expect(url).toContain('start_timestamp=1700000000')
    expect(opts).toMatchObject({ live: true })
  })

  it('destroys the player when the modal closes', async () => {
    const { rerender } = renderWithMantine(
      <PreviewModal opened onClose={() => {}} program={program} channel={{ stream_id: '42' }} accountId={1} />
    )
    await waitFor(() => expect(attachMpegts).toHaveBeenCalled())

    rerender(
      <MantineProvider>
        <PreviewModal opened={false} onClose={() => {}} program={program} channel={{ stream_id: '42' }} accountId={1} />
      </MantineProvider>
    )
    await waitFor(() => expect(destroy).toHaveBeenCalled())
  })
})
