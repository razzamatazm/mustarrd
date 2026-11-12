import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  ActionIcon,
  Box,
  Button,
  CloseButton,
  Group,
  Indicator,
  Popover,
  ScrollArea,
  Stack,
  Text,
  UnstyledButton,
} from '@mantine/core'
import { IconAlertCircle, IconAlertTriangle, IconBell } from '@tabler/icons-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import dayjs from 'dayjs'
import relativeTime from 'dayjs/plugin/relativeTime'

import { notificationsApi } from '../api'

dayjs.extend(relativeTime)

export const NOTIFICATIONS_QUERY_KEY = ['notifications']

function NotificationItem({ item, onOpen, onDismiss, dismissing }) {
  const isError = item.severity === 'error'
  const Icon = isError ? IconAlertCircle : IconAlertTriangle
  return (
    <Group
      align="flex-start"
      wrap="nowrap"
      gap="sm"
      p="xs"
      style={{ borderRadius: 8, background: 'var(--mantine-color-default-hover)' }}
      data-testid="notification-item"
    >
      <Icon size={18} color={isError ? '#fa5252' : '#f59f00'} style={{ flexShrink: 0, marginTop: 2 }} />
      <UnstyledButton
        onClick={() => onOpen(item)}
        disabled={!item.link}
        style={{ flex: 1, minWidth: 0, cursor: item.link ? 'pointer' : 'default' }}
      >
        <Text size="sm" fw={600}>{item.title}</Text>
        <Text size="xs" c="dimmed" style={{ wordBreak: 'break-word' }}>{item.message}</Text>
        {item.created_at ? (
          <Text size="xs" c="dimmed" mt={2}>{dayjs(item.created_at).fromNow()}</Text>
        ) : null}
      </UnstyledButton>
      <CloseButton
        size="sm"
        aria-label={`Dismiss ${item.title}`}
        onClick={() => onDismiss(item.id)}
        disabled={dismissing}
      />
    </Group>
  )
}

export default function NotificationBell({ onNavigate }) {
  const [opened, setOpened] = useState(false)
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data } = useQuery({
    queryKey: NOTIFICATIONS_QUERY_KEY,
    queryFn: notificationsApi.list,
    refetchInterval: 60000,
  })
  const items = data?.items ?? []
  const count = items.length

  const refresh = () => queryClient.invalidateQueries({ queryKey: NOTIFICATIONS_QUERY_KEY })
  const dismissMutation = useMutation({ mutationFn: notificationsApi.dismiss, onSuccess: refresh })
  const dismissAllMutation = useMutation({ mutationFn: notificationsApi.dismissAll, onSuccess: refresh })

  const openItem = (item) => {
    if (!item.link) return
    setOpened(false)
    onNavigate?.()
    navigate(item.link)
  }

  return (
    <Popover opened={opened} onChange={setOpened} position="bottom-end" width={340} shadow="md" withinPortal>
      <Popover.Target>
        <Indicator
          label={count > 9 ? '9+' : count}
          size={16}
          color="red"
          offset={4}
          disabled={count === 0}
        >
          <ActionIcon
            variant="subtle"
            color="gray"
            size="lg"
            aria-label={count ? `Notifications (${count})` : 'Notifications'}
            onClick={() => setOpened((value) => !value)}
          >
            <IconBell size={20} />
          </ActionIcon>
        </Indicator>
      </Popover.Target>
      <Popover.Dropdown p="sm">
        <Group justify="space-between" mb="xs">
          <Text fw={700} size="sm">Notifications</Text>
          {count > 0 ? (
            <Button
              size="compact-xs"
              variant="subtle"
              onClick={() => dismissAllMutation.mutate()}
              loading={dismissAllMutation.isPending}
            >
              Dismiss all
            </Button>
          ) : null}
        </Group>
        {count === 0 ? (
          <Box py="md">
            <Text size="sm" c="dimmed" ta="center">Nothing needs your attention.</Text>
          </Box>
        ) : (
          <ScrollArea.Autosize mah={400} type="auto">
            <Stack gap={6}>
              {items.map((item) => (
                <NotificationItem
                  key={item.id}
                  item={item}
                  onOpen={openItem}
                  onDismiss={(id) => dismissMutation.mutate(id)}
                  dismissing={dismissMutation.isPending}
                />
              ))}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Popover.Dropdown>
    </Popover>
  )
}
