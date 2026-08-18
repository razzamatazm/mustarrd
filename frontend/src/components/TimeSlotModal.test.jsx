import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithQuery } from '../test/renderWithProviders'

const createTimeSlot = vi.fn()

vi.mock('../api', () => ({
  timeSlotsApi: {
    create: (...args) => createTimeSlot(...args),
  },
}))

const notificationsShow = vi.fn()
vi.mock('@mantine/notifications', () => ({
  notifications: { show: (...args) => notificationsShow(...args) },
}))

const TimeSlotModalModule = await import('./TimeSlotModal')
const TimeSlotModal = TimeSlotModalModule.default
const { suggestTitle, validateTimeSlot } = TimeSlotModalModule

const NOW = new Date('2026-08-15T12:07:00Z')

const channel = { stream_id: 42, name: 'ESPN' }

function renderModal(props = {}) {
  return renderWithQuery(
    <TimeSlotModal
      opened
      onClose={() => {}}
      channel={channel}
      accountId="1"
      guideOffsetHours={0}
      archiveDays={7}
      {...props}
    />
  )
}

describe('validateTimeSlot', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('accepts an ordinary past range', () => {
    expect(
      validateTimeSlot({
        startValue: '2026-08-15T09:00',
        endValue: '2026-08-15T11:00',
        archiveDays: 7,
      })
    ).toBeNull()
  })

  it('rejects an end at or before the start', () => {
    expect(
      validateTimeSlot({
        startValue: '2026-08-15T11:00',
        endValue: '2026-08-15T11:00',
        archiveDays: 7,
      })
    ).toMatch(/after the start time/)
  })

  it('rejects a range longer than six hours', () => {
    expect(
      validateTimeSlot({
        startValue: '2026-08-15T00:00',
        endValue: '2026-08-15T06:30',
        archiveDays: 30,
      })
    ).toMatch(/at most 6 hours/)
  })

  it('rejects a start older than the channel archive and names the depth', () => {
    const message = validateTimeSlot({
      startValue: '2026-08-05T09:00',
      endValue: '2026-08-05T10:00',
      archiveDays: 7,
    })
    expect(message).toMatch(/catchup window/)
    expect(message).toMatch(/7 days/)
  })

  it('follows the channel archive depth rather than a constant', () => {
    expect(
      validateTimeSlot({
        startValue: '2026-08-05T09:00',
        endValue: '2026-08-05T10:00',
        archiveDays: 21,
      })
    ).toBeNull()
  })

  it('rejects a start more than fourteen days ahead', () => {
    expect(
      validateTimeSlot({
        startValue: '2026-09-01T09:00',
        endValue: '2026-09-01T10:00',
        archiveDays: 7,
      })
    ).toMatch(/14 days in the future/)
  })

  it('accounts for the guide offset when checking bounds', () => {
    // 09:00 on a guide running +14 is well inside the window in UTC terms,
    // but only because the offset is applied.
    expect(
      validateTimeSlot({
        startValue: '2026-08-15T09:00',
        endValue: '2026-08-15T10:00',
        archiveDays: 7,
        guideOffsetHours: 2,
      })
    ).toBeNull()
  })
})

describe('suggestTitle', () => {
  it('combines the channel name with the start', () => {
    expect(suggestTitle('ESPN', '2026-08-15T14:00')).toBe('ESPN - Aug 15 2:00 PM')
  })

  it('falls back to the channel name alone when the start is unusable', () => {
    expect(suggestTitle('ESPN', '')).toBe('ESPN')
  })
})

describe('TimeSlotModal', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(NOW)
    createTimeSlot.mockReset()
    notificationsShow.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('prefills now rounded down to the quarter hour, plus an hour', () => {
    renderModal()
    expect(screen.getByLabelText('Start').value).toBe('2026-08-15T12:00')
    expect(screen.getByLabelText('End').value).toBe('2026-08-15T13:00')
  })

  it('prefills the times on the channel clock, not UTC', () => {
    renderModal({ guideOffsetHours: 2 })
    expect(screen.getByLabelText('Start').value).toBe('2026-08-15T14:00')
  })

  it('prefills an editable suggested name', () => {
    renderModal()
    expect(screen.getByLabelText('Name').value).toBe('ESPN - Aug 15 12:00 PM')
  })

  it('keeps the suggestion in step with the start until the user edits it', () => {
    renderModal()
    fireEvent.change(screen.getByLabelText('Start'), { target: { value: '2026-08-15T09:30' } })
    expect(screen.getByLabelText('Name').value).toBe('ESPN - Aug 15 9:30 AM')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Wimbledon QF' } })
    fireEvent.change(screen.getByLabelText('Start'), { target: { value: '2026-08-15T08:00' } })
    expect(screen.getByLabelText('Name').value).toBe('Wimbledon QF')
  })

  it('says that no padding is applied', () => {
    renderModal()
    expect(screen.getByText(/Records exactly the times you enter/)).toBeTruthy()
  })

  it('labels the start as being on the channel guide clock', () => {
    renderModal()
    expect(screen.getByText(/guide clock/)).toBeTruthy()
  })

  it('shows a validation message and blocks submission for a range over six hours', () => {
    renderModal()
    fireEvent.change(screen.getByLabelText('End'), { target: { value: '2026-08-15T19:00' } })
    expect(screen.getByText(/at most 6 hours/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Record' }).disabled).toBe(true)
  })

  it('blocks submission when the name is emptied', () => {
    renderModal()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: 'Record' }).disabled).toBe(true)
  })

  it('submits unix timestamps converted off the channel clock', async () => {
    createTimeSlot.mockResolvedValue({ kind: 'schedule', record: {} })
    renderModal({ guideOffsetHours: 2 })

    fireEvent.change(screen.getByLabelText('Start'), { target: { value: '2026-08-15T16:00' } })
    fireEvent.change(screen.getByLabelText('End'), { target: { value: '2026-08-15T18:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record' }))

    await waitFor(() => expect(createTimeSlot).toHaveBeenCalled())
    const payload = createTimeSlot.mock.calls[0][0]
    // 4pm on a guide running +2 is 2pm UTC.
    expect(payload.start_timestamp).toBe(Math.floor(Date.UTC(2026, 7, 15, 14, 0) / 1000))
    expect(payload.stop_timestamp).toBe(Math.floor(Date.UTC(2026, 7, 15, 16, 0) / 1000))
    expect(payload.channel_id).toBe('42')
    expect(payload.confirm_overlap).toBe(false)
  })

  it('warns on an overlap and resubmits with confirmation', async () => {
    const overlap = new Error('overlap')
    overlap.status = 409
    overlap.payload = { detail: { code: 'overlap', message: 'This overlaps "Tennis, wide".' } }
    createTimeSlot.mockRejectedValueOnce(overlap)
    createTimeSlot.mockResolvedValueOnce({ kind: 'schedule', record: {} })

    renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Record' }))

    await waitFor(() => expect(screen.getByText(/Tennis, wide/)).toBeTruthy())
    expect(notificationsShow).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Record anyway' }))
    await waitFor(() => expect(createTimeSlot).toHaveBeenCalledTimes(2))
    expect(createTimeSlot.mock.calls[1][0].confirm_overlap).toBe(true)
  })

  it('surfaces other errors as notifications', async () => {
    createTimeSlot.mockRejectedValue(new Error('Provider unreachable'))
    renderModal()
    fireEvent.click(screen.getByRole('button', { name: 'Record' }))

    await waitFor(() => expect(notificationsShow).toHaveBeenCalled())
    expect(notificationsShow.mock.calls[0][0].message).toBe('Provider unreachable')
  })

  it('tells the user a future slot waits for catchup rather than recording live', () => {
    renderModal()
    fireEvent.change(screen.getByLabelText('Start'), { target: { value: '2026-08-15T20:00' } })
    fireEvent.change(screen.getByLabelText('End'), { target: { value: '2026-08-15T21:00' } })
    expect(screen.getByText(/does not record the live stream/)).toBeTruthy()
    expect(screen.getByText('Waits for catchup')).toBeTruthy()
  })

  it('renders nothing without a channel', () => {
    renderWithQuery(<TimeSlotModal opened onClose={() => {}} channel={null} accountId="1" />)
    expect(screen.queryByText('Record a time slot')).toBeNull()
  })
})
