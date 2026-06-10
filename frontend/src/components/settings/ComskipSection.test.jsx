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

    expect(
      screen.getByText(/Comskip is not enabled\. Turn it on in Post-Processing/)
    ).toBeInTheDocument()
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
    expect(screen.getByRole('checkbox', { name: 'Resolution change' })).toBeChecked()
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

  it('reports custom INI path edits through onChange', () => {
    const { onChange } = renderSection()

    fireEvent.change(screen.getByPlaceholderText('/path/to/comskip.ini'), {
      target: { value: '/tmp/custom.ini' },
    })

    expect(onChange).toHaveBeenCalledWith('comskip_custom_ini_path', '/tmp/custom.ini')
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
})
