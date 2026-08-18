import mpegts from 'mpegts.js'
import Hls from 'hls.js'

// Containers browsers play natively in a <video> tag (codec permitting).
const NATIVE_EXTENSIONS = new Set(['mp4', 'm4v', 'mov', 'webm'])
// MPEG-TS variants we can demux client-side via mpegts.js + MSE.
const TS_EXTENSIONS = new Set(['ts', 'm2ts', 'mts', 'mpegts'])

export function fileExtension(path) {
  if (!path) return ''
  const name = String(path).split('/').pop().split('\\').pop()
  const dot = name.lastIndexOf('.')
  if (dot <= 0) return ''
  return name.slice(dot + 1).toLowerCase()
}

export function mpegtsSupported() {
  return Boolean(mpegts.isSupported())
}

export function hlsJsSupported() {
  return Boolean(Hls.isSupported())
}

export function nativeHlsSupported(video) {
  return Boolean(video && video.canPlayType && video.canPlayType('application/vnd.apple.mpegurl'))
}

// Decide how to play a downloaded file: 'native' (plain src), 'mpegts'
// (mpegts.js demuxes TS into MSE), or 'hls' (server repackages via FFmpeg —
// the catch-all for MKV and anything MSE can't demux).
export function pickEngine(path, { tsSupported = mpegtsSupported() } = {}) {
  const ext = fileExtension(path)
  if (NATIVE_EXTENSIONS.has(ext)) return 'native'
  if (TS_EXTENSIONS.has(ext) && tsSupported) return 'mpegts'
  return 'hls'
}

// Whether a channel needs a Converted preview is a property of this browser,
// not of the channel: the same stream plays in Chrome and fails in Firefox,
// and iOS Safari fails on everything. So the answer is remembered per browser
// rather than on the channel record, where it would push Chrome users onto a
// needless FFmpeg process. Losing it costs one wasted Direct attempt.
const CONVERTED_PREVIEW_PREFIX = 'mustarrd.preview.converted'

function convertedPreviewKey(accountId, channelId) {
  return `${CONVERTED_PREVIEW_PREFIX}.${accountId}.${channelId}`
}

export function previewNeedsConversion(accountId, channelId) {
  if (accountId == null || channelId == null) return false
  try {
    return window.localStorage.getItem(convertedPreviewKey(accountId, channelId)) === '1'
  } catch {
    // Private browsing / storage disabled: fall back to trying Direct first.
    return false
  }
}

export function rememberPreviewNeedsConversion(accountId, channelId) {
  if (accountId == null || channelId == null) return
  try {
    window.localStorage.setItem(convertedPreviewKey(accountId, channelId), '1')
  } catch {
    // Nothing to do: the next preview simply retries the Direct path.
  }
}

// Each attach* returns a destroy() that detaches the engine and releases
// the media element, safe to call more than once.

export function attachMpegts(video, url, { live = false, onError } = {}) {
  // mpegts.js fetches from a worker, where relative URLs cannot resolve.
  const absoluteUrl = new URL(url, window.location.href).toString()
  const player = mpegts.createPlayer(
    { type: 'mpegts', isLive: live, url: absoluteUrl },
    { enableWorker: true, seekType: 'range', lazyLoad: !live },
  )
  player.attachMediaElement(video)
  player.on(mpegts.Events.ERROR, (errorType, errorDetail, errorInfo) => {
    if (onError) onError(`${errorType}: ${errorDetail}`, errorInfo)
  })
  player.load()
  let destroyed = false
  return () => {
    if (destroyed) return
    destroyed = true
    try {
      player.destroy()
    } catch {
      // already detached
    }
  }
}

export function attachHls(video, url, { onError, startPosition = -1 } = {}) {
  if (Hls.isSupported()) {
    const hls = new Hls({ maxBufferLength: 60, startPosition })
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (data?.fatal && onError) {
        onError(data.details || data.type || 'playback failed', data)
      }
    })
    hls.loadSource(url)
    hls.attachMedia(video)
    let destroyed = false
    return () => {
      if (destroyed) return
      destroyed = true
      try {
        hls.destroy()
      } catch {
        // already detached
      }
    }
  }

  if (nativeHlsSupported(video)) {
    // Safari plays the playlist itself, so there is no hls.js error event and
    // no HTTP status to read: a refused stream would otherwise sit as a blank
    // player. The element's own error event is the only signal available.
    const handleError = () => {
      if (onError) onError('playback failed')
    }
    video.addEventListener('error', handleError)
    video.src = url
    return () => {
      video.removeEventListener('error', handleError)
      video.removeAttribute('src')
      video.load()
    }
  }

  if (onError) onError('This browser cannot play streams')
  return () => {}
}
