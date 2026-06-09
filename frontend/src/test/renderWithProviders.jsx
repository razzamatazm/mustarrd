import { render } from '@testing-library/react'
import { MantineProvider } from '@mantine/core'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

export function renderWithMantine(ui) {
  return render(<MantineProvider>{ui}</MantineProvider>)
}

export function renderWithQuery(ui) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchInterval: false },
    },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MantineProvider>{ui}</MantineProvider>
    </QueryClientProvider>
  )
}
