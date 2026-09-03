import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithMantine } from '../../test/renderWithProviders'
import WebhooksSection, { getWebhookErrors } from './WebhooksSection'

const emptyFormData = {
  webhook_url_recording_started: '',
  webhook_url_recording_completed: '',
  webhook_url_recording_failed: '',
  webhook_url_recording_cancelled: '',
  webhook_url_postprocessing_completed: '',
}

function renderSection(overrides = {}) {
  const onChange = vi.fn()
  renderWithMantine(
    <WebhooksSection formData={{ ...emptyFormData, ...overrides }} onChange={onChange} />
  )
  return { onChange }
}

describe('WebhooksSection', () => {
  it('renders all five inputs with their current values', () => {
    renderSection({
      webhook_url_recording_started: 'http://192.168.1.10/started',
      webhook_url_recording_completed: 'https://example.com/done',
      webhook_url_recording_failed: 'http://nas.local:8080/failed',
      webhook_url_recording_cancelled: 'http://127.0.0.1:9000/cancelled',
      webhook_url_postprocessing_completed: 'https://example.com/processed',
    })

    expect(screen.getByLabelText('Recording started')).toHaveValue('http://192.168.1.10/started')
    expect(screen.getByLabelText('Recording finished')).toHaveValue('https://example.com/done')
    expect(screen.getByLabelText('Recording failed')).toHaveValue('http://nas.local:8080/failed')
    expect(screen.getByLabelText('Recording cancelled')).toHaveValue('http://127.0.0.1:9000/cancelled')
    expect(screen.getByLabelText('Processing finished')).toHaveValue('https://example.com/processed')
  })

  it('explains what a webhook does and that an empty box turns it off', () => {
    renderSection()

    expect(screen.getByText(/short JSON message/)).toBeInTheDocument()
    expect(screen.getByText(/Leave a box empty to turn that one off/)).toBeInTheDocument()
    expect(screen.getByText(/can never delay or break a recording/)).toBeInTheDocument()
  })

  it('reports edits through onChange with the right field', () => {
    const { onChange } = renderSection()

    fireEvent.change(screen.getByLabelText('Processing finished'), {
      target: { value: 'http://plex.local:32400/hook' },
    })

    expect(onChange).toHaveBeenCalledWith(
      'webhook_url_postprocessing_completed',
      'http://plex.local:32400/hook'
    )
  })

  it('clears a webhook back to empty', () => {
    const { onChange } = renderSection({ webhook_url_recording_started: 'http://a.example/x' })

    fireEvent.change(screen.getByLabelText('Recording started'), { target: { value: '' } })

    expect(onChange).toHaveBeenCalledWith('webhook_url_recording_started', '')
  })

  it('surfaces an inline error for a bad address', () => {
    renderSection({ webhook_url_recording_failed: 'ftp://example.com/hook' })

    expect(screen.getByText('Web address must start with http:// or https://')).toBeInTheDocument()
  })

  it('surfaces an inline error for a reserved address', () => {
    renderSection({ webhook_url_recording_started: 'http://169.254.169.254/latest/meta-data' })

    expect(
      screen.getByText('That address is reserved and cannot receive a webhook.')
    ).toBeInTheDocument()
  })

  it('does not flag a LAN address', () => {
    renderSection({ webhook_url_recording_completed: 'http://192.168.1.50:8096/jellyfin' })

    expect(screen.queryByText(/reserved/)).not.toBeInTheDocument()
    expect(screen.queryByText(/must start with http/)).not.toBeInTheDocument()
  })
})

describe('getWebhookErrors', () => {
  it('returns no errors when every box is empty', () => {
    expect(getWebhookErrors(emptyFormData)).toEqual({})
  })

  it('returns no errors when formData is null', () => {
    expect(getWebhookErrors(null)).toEqual({})
  })

  it('accepts LAN and loopback addresses', () => {
    const errors = getWebhookErrors({
      ...emptyFormData,
      webhook_url_recording_started: 'http://192.168.1.10:8080/hook',
      webhook_url_recording_completed: 'http://10.0.0.5/hook',
      webhook_url_recording_failed: 'http://172.16.4.2/hook',
      webhook_url_recording_cancelled: 'http://localhost:5678/hook',
      webhook_url_postprocessing_completed: 'http://127.0.0.1:32400/hook',
    })
    expect(errors).toEqual({})
  })

  it('accepts an ordinary https address', () => {
    expect(
      getWebhookErrors({ ...emptyFormData, webhook_url_recording_started: 'https://example.com/a/b?c=d' })
    ).toEqual({})
  })

  it('rejects a non-http scheme', () => {
    const errors = getWebhookErrors({
      ...emptyFormData,
      webhook_url_recording_started: 'ftp://example.com/hook',
    })
    expect(errors.webhook_url_recording_started).toMatch(/http:\/\/ or https:\/\//)
  })

  it('rejects an address with no host', () => {
    const errors = getWebhookErrors({
      ...emptyFormData,
      webhook_url_recording_started: 'http://',
    })
    expect(errors.webhook_url_recording_started).toMatch(/host/)
  })

  it('rejects whitespace inside the address', () => {
    const errors = getWebhookErrors({
      ...emptyFormData,
      webhook_url_recording_completed: 'http://exa mple.com/hook',
    })
    expect(errors.webhook_url_recording_completed).toMatch(/spaces/)
  })

  it('rejects an address longer than 1000 characters', () => {
    const errors = getWebhookErrors({
      ...emptyFormData,
      webhook_url_recording_failed: `https://example.com/${'a'.repeat(1000)}`,
    })
    expect(errors.webhook_url_recording_failed).toMatch(/1000 characters/)
  })

  it('rejects the cloud metadata endpoint and other reserved ranges', () => {
    const errors = getWebhookErrors({
      ...emptyFormData,
      webhook_url_recording_started: 'http://169.254.169.254/latest/meta-data',
      webhook_url_recording_completed: 'http://0.0.0.0:8080/hook',
      webhook_url_recording_failed: 'http://239.1.2.3/hook',
      webhook_url_recording_cancelled: 'http://240.0.0.1/hook',
    })
    expect(Object.keys(errors).sort()).toEqual([
      'webhook_url_recording_cancelled',
      'webhook_url_recording_completed',
      'webhook_url_recording_failed',
      'webhook_url_recording_started',
    ])
  })

  it('ignores surrounding whitespace when a value is otherwise fine', () => {
    expect(
      getWebhookErrors({ ...emptyFormData, webhook_url_recording_started: '  https://example.com/hook  ' })
    ).toEqual({})
  })
})
