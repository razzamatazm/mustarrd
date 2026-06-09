import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

// globals is false in vitest.config.js, so React Testing Library does not
// auto-register its cleanup hook — without this the DOM leaks across tests.
afterEach(cleanup)

// Mantine components rely on browser APIs that jsdom does not implement.
const { getComputedStyle } = window
window.getComputedStyle = (elt) => getComputedStyle(elt)
window.HTMLElement.prototype.scrollIntoView = () => {}

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
})

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

window.ResizeObserver = window.ResizeObserver || ResizeObserverStub
