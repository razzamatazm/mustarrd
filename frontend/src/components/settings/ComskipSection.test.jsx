import { describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithMantine } from '../../test/renderWithProviders'
import ComskipSection, { COMSKIP_DEFAULTS, getComskipErrors } from './ComskipSection'

const baseFormData = {
  comskip_enabled: true,
  ...COMSKIP_DEFAULTS,
  comskip_custom_ini_path: null,
}

function renderSection(overrides = {}, props = {}) {
  const onChange = vi.fn()
  const onResetDefaults = vi.fn()
  renderWithMantine(
    <ComskipSection
      formData={{ ...baseFormData, ...overrides }}
      onChange={onChange}
      onResetDefaults={onResetDefaults}
      {...props}
    />
  )
  return { onChange, onResetDefaults }
}

describe('ComskipSection', () => {
  it('shows the disabled banner and greys out controls when comskip is off', () => {
    renderSection({ comskip_enabled: false })

    expect(screen.getByText(/Comskip is not enabled\. Turn it on in/)).toBeInTheDocument()
    expect(screen.getByText('Post-Processing')).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Black frames' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reset to Defaults' })).toBeDisabled()
  })

  it('hides the banner and enables controls when comskip is on', () => {
    renderSection()

    expect(screen.queryByText(/Comskip is not enabled/)).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'Black frames' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Reset to Defaults' })).toBeEnabled()
  })

  it('decomposes detect_method 107 into the right checkboxes', () => {
    renderSection()

    expect(screen.getByRole('checkbox', { name: 'Black frames' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Logo detection' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Fuzzy logic' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Aspect ratio change' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Silence detection' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Scene change' })).not.toBeChecked()
  })

  it('recomposes the bitmask when a checkbox is toggled', () => {
    const { onChange } = renderSection()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Scene change' }))

    expect(onChange).toHaveBeenCalledWith('comskip_detect_method', 107 + 4)
  })

  it('preserves bits that have no checkbox (e.g. closed captions = 16)', () => {
    const { onChange } = renderSection({ comskip_detect_method: 107 + 16 })

    fireEvent.click(screen.getByRole('checkbox', { name: 'Scene change' }))

    expect(onChange).toHaveBeenCalledWith('comskip_detect_method', 107 + 16 + 4)
  })

  it('shows an inline error when min commercial break exceeds max', () => {
    renderSection({
      comskip_min_commercialbreak: 700,
      comskip_max_commercialbreak: 600,
    })

    expect(
      screen.getByText('Min commercial break must be less than or equal to max commercial break.')
    ).toBeInTheDocument()
  })

  it('calls onResetDefaults from the reset button', () => {
    const { onResetDefaults } = renderSection()

    fireEvent.click(screen.getByRole('button', { name: 'Reset to Defaults' }))

    expect(onResetDefaults).toHaveBeenCalled()
  })

  it('enables custom INI mode and reports path edits through onChange', () => {
    const { onChange } = renderSection({ comskip_use_custom_ini: true })

    expect(screen.getByText(/default config directory is \/app\/config/)).toBeInTheDocument()

    fireEvent.change(screen.getByPlaceholderText('/path/to/comskip.ini'), {
      target: { value: '/tmp/custom.ini' },
    })

    expect(onChange).toHaveBeenCalledWith('comskip_custom_ini_path', '/tmp/custom.ini')
  })

  it('greys out managed controls while custom INI mode is active', () => {
    renderSection({
      comskip_use_custom_ini: true,
      comskip_custom_ini_path: '/tmp/custom.ini',
    })

    expect(screen.getByRole('checkbox', { name: 'Use a custom Comskip INI' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Black frames' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reset to Defaults' })).toBeEnabled()
    expect(screen.getByText(/Custom INI mode is active/)).toBeInTheDocument()
  })

  it('resets from custom mode', () => {
    const { onResetDefaults } = renderSection({
      comskip_use_custom_ini: true,
      comskip_custom_ini_path: '/tmp/custom.ini',
    })

    fireEvent.click(screen.getByRole('button', { name: 'Reset to Defaults' }))

    expect(onResetDefaults).toHaveBeenCalledOnce()
  })

  it('uses a boolean enabled default for connecting logo blocks', () => {
    const { onChange } = renderSection()
    const checkbox = screen.getByRole('checkbox', { name: 'Connect blocks with logo' })

    expect(checkbox).toBeChecked()
    fireEvent.click(checkbox)

    expect(onChange).toHaveBeenCalledWith('comskip_connect_blocks_with_logo', false)
  })

  it('renders the hardware decode picker defaulting to none', () => {
    renderSection()

    expect(screen.getByRole('textbox', { name: 'Hardware decoding' })).toHaveValue('None (software)')
  })

  it('persists the chosen hardware decode mode', async () => {
    const { onChange } = renderSection({}, {
      hwDecodeModes: [
        { id: 'none', name: 'None (software)', available: true },
        { id: 'hwassist', name: 'Hardware assist', available: true },
        { id: 'nvidia', name: 'NVIDIA (CUVID)', available: true },
      ],
    })

    fireEvent.click(screen.getByRole('textbox', { name: 'Hardware decoding' }))
    fireEvent.click(await screen.findByText('NVIDIA (CUVID)'))

    expect(onChange).toHaveBeenCalledWith('comskip_hw_decode_mode', 'nvidia')
  })

  it('keeps the hardware decode picker usable with a custom INI', () => {
    renderSection({ comskip_use_custom_ini: true, comskip_custom_ini_path: '/tmp/my.ini' })

    // Hardware decoding is a command-line flag, not an INI key, so a custom INI
    // must not take it over the way it does the managed tunables.
    expect(screen.getByRole('textbox', { name: 'Hardware decoding' })).toBeEnabled()
    expect(screen.getByRole('checkbox', { name: 'Black frames' })).toBeDisabled()
  })

  it('shows unavailable hardware decode modes rather than hiding them', async () => {
    renderSection({}, {
      hwDecodeModes: [
        { id: 'none', name: 'None (software)', available: true },
        { id: 'hwassist', name: 'Hardware assist', available: false },
        { id: 'nvidia', name: 'NVIDIA (CUVID)', available: false },
      ],
    })

    fireEvent.click(screen.getByRole('textbox', { name: 'Hardware decoding' }))

    expect(await screen.findByText('Hardware assist (not detected)')).toBeInTheDocument()
    expect(screen.getByText('NVIDIA (CUVID) (not detected)')).toBeInTheDocument()
  })

  it('warns that multiple processing threads can change detection', () => {
    renderSection({ comskip_thread_count: 4 })

    expect(screen.getByText('Higher thread counts can change detection results')).toBeInTheDocument()
    expect(screen.getByText(/return to 1 if breaks are missed/)).toBeInTheDocument()
  })
})

describe('getComskipErrors', () => {
  it('returns no errors for defaults', () => {
    expect(getComskipErrors(baseFormData)).toEqual({})
  })

  it('flags inverted commercial break pair', () => {
    const errors = getComskipErrors({
      ...baseFormData,
      comskip_min_commercialbreak: 999,
    })
    expect(errors.commercialbreak).toBeTruthy()
  })

  it('flags inverted commercial size pair', () => {
    const errors = getComskipErrors({
      ...baseFormData,
      comskip_max_commercial_size: 1,
    })
    expect(errors.commercial_size).toBeTruthy()
  })

  it('accepts equal min and max', () => {
    const errors = getComskipErrors({
      ...baseFormData,
      comskip_min_commercialbreak: 100,
      comskip_max_commercialbreak: 100,
    })
    expect(errors).toEqual({})
  })

  it('returns no errors when formData is null', () => {
    expect(getComskipErrors(null)).toEqual({})
  })

  it('requires a path when custom INI mode is enabled', () => {
    const errors = getComskipErrors({
      ...baseFormData,
      comskip_use_custom_ini: true,
      comskip_custom_ini_path: '',
    })
    expect(errors.custom_ini).toBeTruthy()
  })
})
