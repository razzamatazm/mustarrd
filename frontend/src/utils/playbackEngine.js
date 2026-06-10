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
        onError(data.details || data.type || 'playback failed')
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
    video.src = url
    return () => {
      video.removeAttribute('src')
      video.load()
    }
  }

  if (onError) onError('This browser cannot play streams')
  return () => {}
}
