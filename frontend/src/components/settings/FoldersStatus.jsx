import {
  Alert,
  Badge,
  Card,
  Group,
  Loader,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core'
import { useQuery } from '@tanstack/react-query'
import { IconAlertTriangle, IconCircleCheck, IconFolder } from '@tabler/icons-react'

import { settingsApi } from '../../api'

function FolderRow({ label, status }) {
  if (!status) return null
  const ok = status.exists && status.writable
  const badgeLabel = ok ? 'Writable' : status.exists ? 'Not writable' : 'Missing'

  return (
    <Group justify="space-between" wrap="nowrap" align="flex-start">
      <Stack gap={2} style={{ flex: 1, overflow: 'hidden' }}>
        <Text size="sm" fw={500}>{label}</Text>
        <Tooltip label={status.path} openDelay={300}>
          <Text size="xs" c="dimmed" ff="monospace" truncate>
            {status.path}
          </Text>
        </Tooltip>
        {!ok && status.error && (
          <Text size="xs" c="red">{status.error}</Text>
        )}
      </Stack>
      <Badge
        color={ok ? 'green' : 'red'}
        variant="light"
        leftSection={ok ? <IconCircleCheck size={12} /> : <IconAlertTriangle size={12} />}
      >
        {badgeLabel}
      </Badge>
    </Group>
  )
}

export default function FoldersStatus() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['settings', 'folders-status'],
    queryFn: settingsApi.getFoldersStatus,
    refetchInterval: 60000,
  })

  return (
    <Card shadow="sm" padding="md" radius="md" withBorder>
      <Stack gap="sm">
        <Group gap="xs">
          <IconFolder size={18} />
          <Text fw={600}>Recording Folders</Text>
          {isLoading && <Loader size="xs" />}
        </Group>
        {error && (
          <Alert color="red" variant="light" p="xs">
            <Text size="xs">Could not check folder status: {error.message}</Text>
          </Alert>
        )}
        {data && (
          <>
            <FolderRow label="Download folder" status={data.download_folder} />
            <FolderRow label="Completed recordings folder" status={data.completed_folder} />
          </>
        )}
      </Stack>
    </Card>
  )
}
