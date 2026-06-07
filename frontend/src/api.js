const API_BASE = '/api'
let csrfTokenPromise = null

async function getCsrfToken(forceRefresh = false) {
  if (!forceRefresh && csrfTokenPromise) {
    return csrfTokenPromise
  }

  csrfTokenPromise = fetch(`${API_BASE}/auth/csrf`, {
    credentials: 'include',
    headers: {
      Accept: 'application/json',
    },
  })
    .then(async (response) => {
      if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Unable to fetch CSRF token' }))
        throw new Error(String(error?.detail ?? 'Unable to fetch CSRF token'))
      }
      const data = await response.json()
      if (!data?.csrf_token) {
        throw new Error('Unable to fetch CSRF token')
      }
      return data.csrf_token
    })
    .catch((error) => {
      csrfTokenPromise = null
      throw error
    })

  return csrfTokenPromise
}

async function request(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`
  const method = String(options.method || 'GET').toUpperCase()
  let errorPayload = null
  const config = {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  }

  if (config.body && typeof config.body === 'object') {
    config.body = JSON.stringify(config.body)
  }

  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && !config.headers['X-CSRF-Token']) {
    config.headers['X-CSRF-Token'] = await getCsrfToken()
  }

  let response = await fetch(url, config)
  if (response.status === 403 && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    errorPayload = await response.json().catch(() => ({}))
    if ((errorPayload?.detail || '').includes('CSRF')) {
      config.headers['X-CSRF-Token'] = await getCsrfToken(true)
      response = await fetch(url, config)
      errorPayload = null
    }
  }

  if (!response.ok) {
    const error = errorPayload ?? await response.json().catch(() => ({ detail: 'Request failed' }))
    let detail = error?.detail ?? 'Request failed'
    if (typeof detail === 'object' && detail !== null) {
      detail = detail.message || detail.error || JSON.stringify(detail)
    }
    const err = new Error(String(detail))
    err.status = response.status
    err.payload = error
    throw err
  }

  return response.json()
}

// Accounts
export const accountsApi = {
  list: () => request('/accounts'),
  publicList: () => request('/accounts/public'),
  get: (id) => request(`/accounts/${id}`),
  create: (data) => request('/accounts', { method: 'POST', body: data }),
  update: (id, data) => request(`/accounts/${id}`, { method: 'PUT', body: data }),
  delete: (id) => request(`/accounts/${id}`, { method: 'DELETE' }),
  test: (id) => request(`/accounts/${id}/test`, { method: 'POST' }),
}

// Channels & EPG
export const channelsApi = {
  getCategories: (accountId) => request(`/accounts/${accountId}/categories`),
  getChannels: (accountId, categoryId = null, catchupOnly = true) => {
    const params = new URLSearchParams()
    if (categoryId) params.append('category_id', categoryId)
    params.append('catchup_only', catchupOnly)
    return request(`/accounts/${accountId}/channels?${params}`)
  },
  getChannel: (accountId, channelId) => request(`/accounts/${accountId}/channels/${channelId}`),
  getEpg: (accountId, channelId, daysBack = 7, fresh = false) =>
    request(
      `/accounts/${accountId}/channels/${channelId}/epg?days_back=${daysBack}&fresh=${fresh ? 'true' : 'false'}`
    ),
  getCatchup: (accountId, channelId, daysBack = 7) =>
    request(`/accounts/${accountId}/channels/${channelId}/catchup?days_back=${daysBack}`),
}

// EPG Search
export const epgApi = {
  search: (accountId, query, limit = 100, offset = 0) =>
    request(
      `/epg/search?account_id=${accountId}&q=${encodeURIComponent(query)}&limit=${limit}&offset=${offset}`
    ),
  status: () => request('/epg/status'),
  refresh: (force = false) => request('/epg/refresh', { method: 'POST', body: { force } }),
}

// Downloads
export const downloadsApi = {
  list: () => request('/downloads'),
  getQueue: () => request('/downloads/queue'),
  getHistory: () => request('/downloads/history'),
  get: (id) => request(`/downloads/${id}`),
  create: (data) => request('/downloads', { method: 'POST', body: data }),
  cancel: (id) => request(`/downloads/${id}`, { method: 'DELETE' }),
  retry: (id) => request(`/downloads/${id}/retry`, { method: 'POST' }),
  previewFilename: (data) => request('/downloads/preview-filename', { method: 'POST', body: data }),
  diskSpace: () => request('/downloads/disk-space'),
  clearFinished: () => request('/downloads/finished', { method: 'DELETE' }),
  upcoming: () => request('/downloads/upcoming'),
  failedCount: (since) => {
    const params = since != null ? `?since=${since}` : ''
    return request(`/downloads/failed-count${params}`)
  },
}

// Schedules
export const schedulesApi = {
  list: () => request('/schedules'),
  create: (data) => request('/schedules', { method: 'POST', body: data }),
  cancel: (id) => request(`/schedules/${id}`, { method: 'DELETE' }),
}

// VOD (On Demand)
export const vodApi = {
  getMovieCategories: (accountId) => request(`/vod/movies/categories?account_id=${accountId}`),
  getMovies: (accountId, categoryId = null) => {
    const params = new URLSearchParams()
    params.append('account_id', accountId)
    if (categoryId) params.append('category_id', categoryId)
    return request(`/vod/movies?${params}`)
  },
  getMovieInfo: (accountId, vodId) =>
    request(`/vod/movies/${vodId}?account_id=${accountId}`),
  downloadMovie: (data) => request('/vod/movies/download', { method: 'POST', body: data }),
  getSeriesCategories: (accountId) => request(`/vod/series/categories?account_id=${accountId}`),
  getSeries: (accountId, categoryId = null) => {
    const params = new URLSearchParams()
    params.append('account_id', accountId)
    if (categoryId) params.append('category_id', categoryId)
    return request(`/vod/series?${params}`)
  },
  getSeriesInfo: (accountId, seriesId) =>
    request(`/vod/series/${seriesId}?account_id=${accountId}`),
  downloadSeries: (data) => request('/vod/series/download', { method: 'POST', body: data }),
}

// Settings
export const settingsApi = {
  get: () => request('/settings'),
  getPublic: () => request('/settings/public'),
  update: (data) => request('/settings', { method: 'PUT', body: data }),
  getTemplates: () => request('/settings/templates'),
  getTools: () => request('/settings/tools'),
}

// Authentication
export const authApi = {
  status: () => request('/auth/status'),
  adminBootstrapStatus: () => request('/auth/admin-bootstrap/status'),
  adminBootstrapComplete: (username, legacyPassword) =>
    request('/auth/admin-bootstrap/complete', {
      method: 'POST',
      body: { username, legacy_password: legacyPassword },
    }),
  setup: (password, username = null) =>
    request('/auth/setup', { method: 'POST', body: { password, username } }),
  loginCredentials: (username, password) =>
    request('/auth/login-credentials', { method: 'POST', body: { username, password } }),
  login: (password) => request('/auth/login', { method: 'POST', body: { password } }),
  userLogin: (username, password) =>
    request('/auth/user/login', { method: 'POST', body: { username, password } }),
  plexStart: () => request('/auth/plex/login/start', { method: 'POST' }),
  plexComplete: (pinId) =>
    request('/auth/plex/login/complete', { method: 'POST', body: { pin_id: pinId } }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  changePassword: (currentPassword, newPassword) =>
    request('/auth/change-password', {
      method: 'POST',
      body: { current_password: currentPassword, new_password: newPassword },
    }),
  getPreferences: () => request('/auth/preferences'),
  updatePreferences: (showFuturePrograms) =>
    request('/auth/preferences', { method: 'PUT', body: { show_future_programs: showFuturePrograms } }),
  validateSetupToken: (token) => request(`/auth/user/setup/${encodeURIComponent(token)}`),
  completeSetupToken: (token, password) =>
    request(`/auth/user/setup/${encodeURIComponent(token)}`, { method: 'POST', body: { password } }),
  generateSetupLink: (userId) => request(`/admin/users/${userId}/setup-link`, { method: 'POST' }),
}

export const adminUsersApi = {
  list: () => request('/admin/users'),
  create: (data) => request('/admin/users', { method: 'POST', body: data }),
  update: (id, data) => request(`/admin/users/${id}`, { method: 'PATCH', body: data }),
  delete: (id) => request(`/admin/users/${id}`, { method: 'DELETE' }),
}

export const adminPlexApi = {
  get: () => request('/admin/plex/integration'),
  save: (data) => request('/admin/plex/integration', { method: 'PUT', body: data }),
  disconnect: () => request('/admin/plex/disconnect', { method: 'POST' }),
  connectStart: () => request('/admin/plex/connect/start', { method: 'POST' }),
  connectComplete: (pinId) => request('/admin/plex/connect/complete', { method: 'POST', body: { pin_id: pinId } }),
  listResources: () => request('/admin/plex/resources'),
  listLibraries: (resourceId, payload = null) =>
    request(`/admin/plex/resources/${encodeURIComponent(resourceId)}/libraries`, {
      method: 'POST',
      body: payload || {},
    }),
  test: (data) => request('/admin/plex/server/test', { method: 'POST', body: data }),
  listUsers: () => request('/admin/plex/server/users'),
}

export const onboardingApi = {
  status: () => request('/onboarding/status'),
  applyProcessingProfile: (profile) =>
    request('/onboarding/processing-profile', {
      method: 'POST',
      body: { profile },
    }),
  setComskipPolicy: (enabled, acknowledgeUnavailable = false) =>
    request('/onboarding/comskip-policy', {
      method: 'POST',
      body: { enabled, acknowledge_unavailable: acknowledgeUnavailable },
    }),
  dismiss: () => request('/onboarding/dismiss', { method: 'POST' }),
}

// Backend Logs
export const logsApi = {
  list: (limit = 300, source = null, level = null, view = 'basic') => {
    const params = new URLSearchParams()
    params.append('limit', String(limit))
    if (source) params.append('source', source)
    if (level) params.append('level', level)
    params.append('view', view)
    return request(`/logs?${params.toString()}`)
  },
}

// WebSocket for download progress
export function createDownloadWebSocket(onMessage, onError = null) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${window.location.host}/api/downloads/ws`

  const ws = new WebSocket(wsUrl)

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    onMessage(data)
  }

  ws.onerror = (error) => {
    if (onError) onError(error)
  }

  return ws
}

export function createLogsWebSocket(onMessage, onError = null) {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const wsUrl = `${protocol}//${window.location.host}/api/logs/ws`
  const ws = new WebSocket(wsUrl)

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    onMessage(data)
  }

  ws.onerror = (error) => {
    if (onError) onError(error)
  }

  return ws
}
