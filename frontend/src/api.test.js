import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

function jsonResponse(data, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    json: async () => data,
  }
}

// The api module caches the CSRF token promise at module level, so each test
// re-imports a fresh copy.
async function freshApi() {
  vi.resetModules()
  return import('./api')
}

describe('api request layer', () => {
  let fetchMock

  beforeEach(() => {
    fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends GET requests without a CSRF token', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock.mockResolvedValueOnce(jsonResponse([{ id: 1 }]))

    const result = await downloadsApi.list()

    expect(result).toEqual([{ id: 1 }])
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, config] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/downloads')
    expect(config.credentials).toBe('include')
    expect(config.headers['X-CSRF-Token']).toBeUndefined()
  })

  it('fetches a CSRF token before unsafe requests and stringifies the body', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'tok-1' }))
      .mockResolvedValueOnce(jsonResponse({ id: 7 }))

    const result = await downloadsApi.create({ channel_id: 5 })

    expect(result).toEqual({ id: 7 })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/csrf')
    const [url, config] = fetchMock.mock.calls[1]
    expect(url).toBe('/api/downloads')
    expect(config.method).toBe('POST')
    expect(config.headers['X-CSRF-Token']).toBe('tok-1')
    expect(config.body).toBe(JSON.stringify({ channel_id: 5 }))
  })

  it('reuses the cached CSRF token across unsafe requests', async () => {
    const { downloadsApi, schedulesApi } = await freshApi()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'tok-1' }))
      .mockResolvedValueOnce(jsonResponse({ id: 1 }))
      .mockResolvedValueOnce(jsonResponse({ id: 2 }))

    await downloadsApi.create({ a: 1 })
    await schedulesApi.create({ b: 2 })

    const csrfCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/auth/csrf')
    expect(csrfCalls).toHaveLength(1)
  })

  it('refreshes the token and retries once on a CSRF 403', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'stale' }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'CSRF token invalid' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'fresh' }))
      .mockResolvedValueOnce(jsonResponse({ id: 9 }))

    const result = await downloadsApi.create({ a: 1 })

    expect(result).toEqual({ id: 9 })
    expect(fetchMock).toHaveBeenCalledTimes(4)
    const retryConfig = fetchMock.mock.calls[3][1]
    expect(retryConfig.headers['X-CSRF-Token']).toBe('fresh')
  })

  it('does not retry a non-CSRF 403', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'tok' }))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Forbidden' }, { ok: false, status: 403 }))

    await expect(downloadsApi.create({})).rejects.toThrow('Forbidden')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('throws an error carrying status and payload detail', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: 'Not found' }, { ok: false, status: 404 })
    )

    const error = await downloadsApi.get(123).catch((e) => e)

    expect(error).toBeInstanceOf(Error)
    expect(error.message).toBe('Not found')
    expect(error.status).toBe(404)
    expect(error.payload).toEqual({ detail: 'Not found' })
  })

  it('flattens object error details to their message', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: { message: 'Disk full', code: 507 } }, { ok: false, status: 507 })
    )

    await expect(downloadsApi.list()).rejects.toThrow('Disk full')
  })

  it('falls back to a generic message when the error body is not JSON', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => {
        throw new Error('bad json')
      },
    })

    await expect(downloadsApi.list()).rejects.toThrow('Request failed')
  })

  it('retries the CSRF fetch after a failed attempt instead of caching the rejection', async () => {
    const { downloadsApi } = await freshApi()
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: 'nope' }, { ok: false, status: 500 }))
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'tok-2' }))
      .mockResolvedValueOnce(jsonResponse({ id: 1 }))

    await expect(downloadsApi.create({})).rejects.toThrow('nope')

    const result = await downloadsApi.create({})
    expect(result).toEqual({ id: 1 })
    const csrfCalls = fetchMock.mock.calls.filter(([url]) => url === '/api/auth/csrf')
    expect(csrfCalls).toHaveLength(2)
  })

  it('encodes query parameters in epg search', async () => {
    const { epgApi } = await freshApi()
    fetchMock.mockResolvedValueOnce(jsonResponse({ results: [] }))

    await epgApi.search(3, 'news & weather')

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/epg/search?account_id=3&q=news%20%26%20weather&limit=100&offset=0'
    )
  })
})
