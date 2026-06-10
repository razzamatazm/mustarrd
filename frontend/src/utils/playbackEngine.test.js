import { describe, expect, it, vi, beforeEach } from 'vitest'

const mpegtsMock = {
  isSupported: vi.fn(() => true),
  createPlayer: vi.fn(),
  Events: { ERROR: 'error' },
}

const hlsInstances = []
class HlsMock {
  static isSupported = vi.fn(() => true)
  static Events = { ERROR: 'hlsError' }

  constructor() {
    this.handlers = {}
    hlsInstances.push(this)
  }

  on(event, handler) {
    this.handlers[event] = handler
  }

  loadSource = vi.fn()
  attachMedia = vi.fn()
  destroy = vi.fn()
}

vi.mock('mpegts.js', () => ({ default: mpegtsMock }))
vi.mock('hls.js', () => ({ default: HlsMock }))

const { attachHls, attachMpegts, fileExtension, pickEngine } = await import('./playbackEngine')

beforeEach(() => {
  vi.clearAllMocks()
  hlsInstances.length = 0
})

describe('fileExtension', () => {
  it('extracts lowercase extension from a unix path', () => {
    expect(fileExtension('/data/completed/Show S01E01.MKV')).toBe('mkv')
  })

  it('extracts extension from a windows path', () => {
    expect(fileExtension('C:\\media\\show.Ts')).toBe('ts')
  })

  it('returns empty string for no extension, dotfiles, and empty input', () => {
    expect(fileExtension('/data/showfile')).toBe('')
    expect(fileExtension('/data/.hidden')).toBe('')
    expect(fileExtension('')).toBe('')
    expect(fileExtension(null)).toBe('')
  })
})

describe('pickEngine', () => {
  it('plays mp4-family containers natively', () => {
    expect(pickEngine('/x/a.mp4', { tsSupported: true })).toBe('native')
    expect(pickEngine('/x/a.m4v', { tsSupported: true })).toBe('native')
    expect(pickEngine('/x/a.webm', { tsSupported: true })).toBe('native')
  })

  it('uses mpegts.js for TS files when MSE is available', () => {
    expect(pickEngine('/x/a.ts', { tsSupported: true })).toBe('mpegts')
    expect(pickEngine('/x/a.m2ts', { tsSupported: true })).toBe('mpegts')
  })

  it('falls back to HLS for TS files without MSE', () => {
    expect(pickEngine('/x/a.ts', { tsSupported: false })).toBe('hls')
  })

  it('routes mkv and unknown containers to HLS', () => {
    expect(pickEngine('/x/a.mkv', { tsSupported: true })).toBe('hls')
    expect(pickEngine('/x/a.avi', { tsSupported: true })).toBe('hls')
    expect(pickEngine('/x/noext', { tsSupported: true })).toBe('hls')
  })
})

describe('attachMpegts', () => {
  it('creates, attaches, loads, and destroys exactly once', () => {
    const player = {
      attachMediaElement: vi.fn(),
      on: vi.fn(),
      load: vi.fn(),
      destroy: vi.fn(),
    }
    mpegtsMock.createPlayer.mockReturnValue(player)
    const video = {}

    const destroy = attachMpegts(video, '/url.ts', { live: true })

    // URL must be absolutized: mpegts.js fetches from a worker where
    // relative URLs fail to parse.
    expect(mpegtsMock.createPlayer).toHaveBeenCalledWith(
      { type: 'mpegts', isLive: true, url: expect.stringMatching(/^https?:\/\/.+\/url\.ts$/) },
      expect.any(Object),
    )
    expect(player.attachMediaElement).toHaveBeenCalledWith(video)
    expect(player.load).toHaveBeenCalled()

    destroy()
    destroy()
    expect(player.destroy).toHaveBeenCalledTimes(1)
  })

  it('reports player errors through onError', () => {
    const player = {
      attachMediaElement: vi.fn(),
      on: vi.fn(),
      load: vi.fn(),
      destroy: vi.fn(),
    }
    mpegtsMock.createPlayer.mockReturnValue(player)
    const onError = vi.fn()

    attachMpegts({}, '/url.ts', { onError })
    const handler = player.on.mock.calls.find(([event]) => event === 'error')[1]
    handler('NetworkError', 'Exception', { code: 429, msg: 'Too Many Requests' })

    expect(onError).toHaveBeenCalledWith('NetworkError: Exception', { code: 429, msg: 'Too Many Requests' })
  })
})

describe('attachHls', () => {
  it('uses hls.js when supported and reports fatal errors', () => {
    HlsMock.isSupported.mockReturnValue(true)
    const onError = vi.fn()
    const video = {}

    const destroy = attachHls(video, '/playlist.m3u8', { onError })
    const hls = hlsInstances[0]
    expect(hls.loadSource).toHaveBeenCalledWith('/playlist.m3u8')
    expect(hls.attachMedia).toHaveBeenCalledWith(video)

    hls.handlers[HlsMock.Events.ERROR]('hlsError', { fatal: false, details: 'minor' })
    expect(onError).not.toHaveBeenCalled()
    hls.handlers[HlsMock.Events.ERROR]('hlsError', { fatal: true, details: 'fatalError' })
    expect(onError).toHaveBeenCalledWith('fatalError')

    destroy()
    destroy()
    expect(hls.destroy).toHaveBeenCalledTimes(1)
  })

  it('falls back to native HLS via canPlayType', () => {
    HlsMock.isSupported.mockReturnValue(false)
    const video = {
      canPlayType: vi.fn(() => 'maybe'),
      removeAttribute: vi.fn(),
      load: vi.fn(),
    }

    const destroy = attachHls(video, '/playlist.m3u8', {})
    expect(video.src).toBe('/playlist.m3u8')

    destroy()
    expect(video.removeAttribute).toHaveBeenCalledWith('src')
  })

  it('reports unsupported browsers', () => {
    HlsMock.isSupported.mockReturnValue(false)
    const onError = vi.fn()
    const video = { canPlayType: vi.fn(() => '') }

    attachHls(video, '/playlist.m3u8', { onError })
    expect(onError).toHaveBeenCalled()
  })
})
