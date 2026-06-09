import { describe, expect, it } from 'vitest'
import { renderWithMantine } from '../test/renderWithProviders'
import ProgressBar from './ProgressBar'

function getFill(container) {
  return container.querySelector('.progress-bar-fill')
}

describe('ProgressBar', () => {
  it('renders the fill at the given progress', () => {
    const { container } = renderWithMantine(<ProgressBar progress={42} />)
    expect(getFill(container).style.width).toBe('42%')
  })

  it('clamps progress above 100', () => {
    const { container } = renderWithMantine(<ProgressBar progress={150} />)
    expect(getFill(container).style.width).toBe('100%')
  })

  it('clamps negative progress to 0', () => {
    const { container } = renderWithMantine(<ProgressBar progress={-20} />)
    expect(getFill(container).style.width).toBe('0%')
  })

  it('defaults to 0 when no progress is given', () => {
    const { container } = renderWithMantine(<ProgressBar />)
    expect(getFill(container).style.width).toBe('0%')
  })

  it('uses the indeterminate animation class when indeterminate', () => {
    const { container } = renderWithMantine(<ProgressBar indeterminate />)
    const fill = getFill(container)
    expect(fill.classList.contains('progress-bar-indeterminate')).toBe(true)
    expect(fill.style.width).toBe('100%')
  })
})
