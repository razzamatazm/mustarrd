import { useState } from 'react'
import {
  Title,
  Button,
  Card,
  Flex,
  Group,
  Text,
  Stack,
  Modal,
  TextInput,
  Badge,
  ActionIcon,
  Menu,
  Loader,
  Alert,
  NumberInput,
  Select,
  Box,
  Tooltip,
} from '@mantine/core'
import { useDisclosure } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  IconPlus,
  IconDotsVertical,
  IconEdit,
  IconTrash,
  IconPlugConnected,
  IconAlertCircle,
  IconServer,
  IconRefresh,
  IconStar,
} from '@tabler/icons-react'
import dayjs from 'dayjs'

import { accountsApi, epgApi, settingsApi } from '../../api'

function timeAgo(dateStr) {
  if (!dateStr) return null
  const diff = Date.now() - new Date(dateStr).getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function getGuideOffsetLabel(account) {
  const offset = Number(account?.guide_offset_hours || 0)
  return offset ? `Guide offset: ${offset >= 0 ? '+' : ''}${offset}h` : 'Guide offset: 0h'
}

function getCatchupLabel(account) {
  const count = Number(account.catchup_channel_count)
  if (count <= 0) return 'Catchup: not available'
  const maxDays = Number(account.catchup_max_archive_days || 0)
  const channels = `${count.toLocaleString()} channel${count === 1 ? '' : 's'}`
  return maxDays > 0
    ? `Catchup: ${channels} · up to ${maxDays} day${maxDays === 1 ? '' : 's'}`
    : `Catchup: ${channels}`
}

export const CATCHUP_URL_STYLE_OPTIONS = [
  { value: 'auto', label: 'Automatic' },
  { value: 'path', label: 'Path (/timeshift/...)' },
  { value: 'query', label: 'Query (/streaming/timeshift.php)' },
]

export function catchupStyleLabel(account) {
  const style = account?.catchup_url_style || 'auto'
  if (style === 'path') return 'Catchup URL: path style'
  if (style === 'query') return 'Catchup URL: query style'
  const resolved = account?.catchup_url_style_resolved
  if (resolved === 'query') return 'Automatic — using query style'
  if (resolved === 'path') return 'Automatic — using path style'
  return 'Automatic — style not yet determined'
}

function AccountForm({ account, onSubmit, onCancel, isLoading }) {
  const [formData, setFormData] = useState({
    name: account?.name || '',
    server_url: account?.server_url || '',
    username: account?.username || '',
    password: account?.password || '',
    guide_offset_hours: account?.guide_offset_hours || 0,
    catchup_url_style: account?.catchup_url_style || 'auto',
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    const payload = { ...formData }
    if (account && !payload.password) {
      delete payload.password
    }
    onSubmit(payload)
  }

  return (
    <form onSubmit={handleSubmit}>
      <Stack>
        <TextInput
          label="Account Name"
          placeholder="My IPTV Provider"
          required
          value={formData.name}
          onChange={(e) => setFormData({ ...formData, name: e.target.value })}
        />
        <TextInput
          label="Server URL"
          placeholder="https://provider.example.com"
          required
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          inputMode="url"
          value={formData.server_url}
          onChange={(e) => setFormData({ ...formData, server_url: e.target.value.toLowerCase() })}
        />
        <TextInput
          label="Username"
          required
          value={formData.username}
          onChange={(e) => setFormData({ ...formData, username: e.target.value })}
        />
        <TextInput
          label="Password"
          type="password"
          required={!account}
          value={formData.password}
          onChange={(e) => setFormData({ ...formData, password: e.target.value })}
        />
        <NumberInput
          label="Guide Alignment Offset (hours)"
          description="Shifts the displayed guide times for this account only. Downloads and schedules still target the same underlying program record."
          min={-12}
          max={12}
          step={1}
          value={formData.guide_offset_hours}
          onChange={(value) =>
            setFormData({
              ...formData,
              guide_offset_hours: typeof value === 'number' ? value : 0,
            })}
        />
        <Select
          label="Catchup URL style"
          description="Most providers work on Automatic. Only change this if your recordings keep failing and your provider needs one specific link format."
          data={CATCHUP_URL_STYLE_OPTIONS}
          allowDeselect={false}
          value={formData.catchup_url_style}
          onChange={(value) =>
            setFormData({
              ...formData,
              catchup_url_style: value || 'auto',
            })}
        />
        <Group justify="flex-end" mt="md">
          <Button variant="subtle" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="submit" loading={isLoading}>
            {account ? 'Update' : 'Add Account'}
          </Button>
        </Group>
      </Stack>
    </form>
  )
}

function AccountCard({ account, isDefault, onSetDefault, onEdit, onDelete, onTest, defaultPending, testPending }) {
  const isExpired = account.expiration_date && dayjs(account.expiration_date).isBefore(dayjs())
  const daysUntilExpiry = account.expiration_date
    ? dayjs(account.expiration_date).diff(dayjs(), 'day')
    : null
  const isExpiringSoon = Boolean(account.expiration_date) && !isExpired && daysUntilExpiry <= 7

  return (
    <Card shadow="sm" padding="md" radius="md" withBorder>
      <Group justify="space-between" wrap="nowrap" mb={2}>
        <Group gap={8} wrap="nowrap" style={{ minWidth: 0 }}>
          <Text fw={600} size="sm" truncate>{account.name}</Text>
          {isDefault && (
            <Badge color="yellow" variant="light">
              Default
            </Badge>
          )}
          {!account.is_active && (
            <Badge color="gray" variant="light">
              Disabled
            </Badge>
          )}
        </Group>
        <Menu shadow="md" width={150}>
          <Menu.Target>
            <ActionIcon variant="subtle" aria-label="Account actions">
              <IconDotsVertical size={16} />
            </ActionIcon>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item
              leftSection={<IconStar size={14} />}
              onClick={() => onSetDefault(account)}
              disabled={isDefault || defaultPending}
            >
              {isDefault ? 'Default Account' : 'Set as Default'}
            </Menu.Item>
            <Menu.Item leftSection={<IconPlugConnected size={14} />} onClick={() => onTest(account)}>
              Test Connection
            </Menu.Item>
            <Menu.Item leftSection={<IconEdit size={14} />} onClick={() => onEdit(account)}>
              Edit
            </Menu.Item>
            <Menu.Divider />
            <Menu.Item color="red" leftSection={<IconTrash size={14} />} onClick={() => onDelete(account)}>
              Delete
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </Group>

      <Text size="xs" c="dimmed" ff="monospace" truncate title={account.server_url}>
        {account.server_url}
      </Text>
      <Text size="xs" c="dimmed" mt={2}>
        User: {account.username}
      </Text>

      <Group mt="md" gap={6}>
        {account.catchup_channel_count != null && (
          <Badge variant="outline" size="sm" color="blue">
            {getCatchupLabel(account)}
          </Badge>
        )}
        {account.vod_available != null && (
          <Badge variant="outline" size="sm" color={account.vod_available ? 'teal' : 'gray'}>
            {account.vod_available ? 'VOD ✓' : 'VOD ✗'}
          </Badge>
        )}
        <Badge variant="light" size="sm" color="indigo">
          {catchupStyleLabel(account)}
        </Badge>
        {Number(account?.guide_offset_hours || 0) !== 0 && (
          <Badge variant="light" size="sm" color="grape">
            {getGuideOffsetLabel(account)}
          </Badge>
        )}
        {account.max_connections && (
          <Badge variant="outline" size="sm">
            {account.active_connections || 0}/{account.max_connections} connections
          </Badge>
        )}
        {isExpiringSoon && (
          <Tooltip label={`Subscription expires ${dayjs(account.expiration_date).format('MMM D, YYYY')}`}>
            <Badge variant="filled" size="sm" color="yellow">
              {daysUntilExpiry === 0
                ? 'Expires today'
                : `Expires in ${daysUntilExpiry} day${daysUntilExpiry === 1 ? '' : 's'}`}
            </Badge>
          </Tooltip>
        )}
        {account.expiration_date && (
          <Badge variant="outline" size="sm" color={isExpired ? 'red' : 'yellow'}>
            {isExpired
              ? `Expired ${dayjs(account.expiration_date).format('MMM D, YYYY')}`
              : `Expires ${dayjs(account.expiration_date).format('MMM D, YYYY')}`}
          </Badge>
        )}
      </Group>

      <Stack mt="sm" gap={2}>
        <Group gap={7} align="center" wrap="nowrap">
          <Box
            style={{
              width: 8,
              height: 8,
              borderRadius: '50%',
              flexShrink: 0,
              backgroundColor:
                account.last_connection_ok === true
                  ? 'var(--mantine-color-green-6)'
                  : account.last_connection_ok === false
                  ? 'var(--mantine-color-red-6)'
                  : 'var(--mantine-color-gray-5)',
            }}
          />
          <Text
            size="xs"
            truncate
            style={{ minWidth: 0 }}
            c={
              account.last_connection_ok === true
                ? 'green'
                : account.last_connection_ok === false
                ? 'red'
                : 'dimmed'
            }
          >
            {account.last_connection_ok === true
              ? 'Connected'
              : account.last_connection_ok === false
              ? isExpired
                ? 'Subscription Expired'
                : 'Unreachable'
              : 'Not checked yet'}
            {account.last_connection_checked_at
              ? ` · ${timeAgo(account.last_connection_checked_at)}`
              : ''}
          </Text>
          <Group gap={4} wrap="nowrap" ml="auto">
            <Button
              size="compact-xs"
              variant="subtle"
              color="gray"
              leftSection={<IconPlugConnected size={13} />}
              onClick={() => onTest(account)}
              loading={testPending}
            >
              Test
            </Button>
            <Button
              size="compact-xs"
              variant="subtle"
              color="gray"
              leftSection={<IconEdit size={13} />}
              onClick={() => onEdit(account)}
            >
              Edit
            </Button>
          </Group>
        </Group>
        {account.last_connection_ok === false && (
          <Text size="xs" c="dimmed" ml={14}>
            {isExpired
              ? 'Subscription has expired. Renew with your IPTV provider to continue.'
              : account.last_connection_error || 'Cannot reach provider.'}
          </Text>
        )}
      </Stack>
    </Card>
  )
}

export default function AccountsSection({ showTitle = true }) {
  const queryClient = useQueryClient()
  const [modalOpened, { open: openModal, close: closeModal }] = useDisclosure(false)
  const [editingAccount, setEditingAccount] = useState(null)

  const { data: accounts, isLoading, error } = useQuery({
    queryKey: ['accounts'],
    queryFn: accountsApi.list,
  })
  const { data: appSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  })
  const { data: epgStatus } = useQuery({
    queryKey: ['epg', 'status'],
    queryFn: epgApi.status,
    refetchInterval: 5000,
  })

  const createMutation = useMutation({
    mutationFn: accountsApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', 'public'] })
      closeModal()
      setEditingAccount(null)
      notifications.show({
        title: 'Success',
        message: 'Account added successfully',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Error',
        message: error.message,
        color: 'red',
      })
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }) => accountsApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', 'public'] })
      closeModal()
      setEditingAccount(null)
      notifications.show({
        title: 'Success',
        message: 'Account updated successfully',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Error',
        message: error.message,
        color: 'red',
      })
    },
  })

  const deleteMutation = useMutation({
    mutationFn: accountsApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', 'public'] })
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['settings', 'public'] })
      notifications.show({
        title: 'Success',
        message: 'Account deleted',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Error',
        message: error.message,
        color: 'red',
      })
    },
  })

  const testMutation = useMutation({
    mutationFn: accountsApi.test,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', 'public'] })
      notifications.show({
        title: 'Connection Successful',
        message: 'Server connection verified',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Connection Failed',
        message: error.message,
        color: 'red',
      })
    },
  })

  const forceRefreshMutation = useMutation({
    mutationFn: () => epgApi.refresh(true),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['epg', 'status'] })
      notifications.show({
        title: 'Force EPG Refresh Started',
        message: 'Guide data is being rebuilt for all active accounts.',
        color: 'yellow',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Refresh Failed',
        message: error.message,
        color: 'red',
      })
    },
  })

  const setDefaultAccountMutation = useMutation({
    mutationFn: (accountId) => settingsApi.update({ default_account_id: accountId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['settings', 'public'] })
      queryClient.invalidateQueries({ queryKey: ['accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts', 'public'] })
      notifications.show({
        title: 'Default Updated',
        message: 'Default account saved',
        color: 'green',
      })
    },
    onError: (error) => {
      notifications.show({
        title: 'Update Failed',
        message: error.message,
        color: 'red',
      })
    },
  })

  const handleAddClick = () => {
    setEditingAccount(null)
    openModal()
  }

  const handleEdit = (account) => {
    setEditingAccount(account)
    openModal()
  }

  const handleDelete = (account) => {
    if (confirm(`Delete account "${account.name}"?`)) {
      deleteMutation.mutate(account.id)
    }
  }

  const handleTest = (account) => {
    testMutation.mutate(account.id)
  }

  const handleSetDefault = (account) => {
    setDefaultAccountMutation.mutate(account.id)
  }

  const handleForceRefresh = () => {
    if (epgStatus?.running) {
      notifications.show({
        title: 'Refresh Already Running',
        message: 'Wait for the current EPG refresh to finish.',
        color: 'yellow',
      })
      return
    }

    if (!confirm('Force refresh will clear stored EPG rows and rebuild guide data. Continue?')) {
      return
    }

    forceRefreshMutation.mutate()
  }

  const handleSubmit = (data) => {
    if (editingAccount) {
      updateMutation.mutate({ id: editingAccount.id, data })
    } else {
      createMutation.mutate(data)
    }
  }

  if (isLoading) {
    return (
      <Stack align="center" justify="center" h={200}>
        <Loader />
      </Stack>
    )
  }

  if (error) {
    return (
      <Alert icon={<IconAlertCircle size={16} />} title="Error" color="red">
        Failed to load accounts: {error.message}
      </Alert>
    )
  }

  return (
    <Stack>
      <Flex
        direction={{ base: 'column', sm: 'row' }}
        justify={{ sm: 'space-between' }}
        align={{ sm: 'flex-start' }}
        gap="xs"
      >
        <Stack gap={2}>
          {showTitle ? (
            <Title order={2}>Provider Accounts</Title>
          ) : (
            <Text fw={700} size="lg">Provider Accounts</Text>
          )}
          <Text size="sm" c="dimmed">Mustarrd reads your channel list and program guide from these IPTV accounts.</Text>
        </Stack>
        {accounts?.length > 0 && (
          <Group gap="xs" style={{ flexShrink: 0 }}>
            <Button
              size="xs"
              variant="light"
              color="orange"
              leftSection={<IconRefresh size={14} />}
              onClick={handleForceRefresh}
              loading={forceRefreshMutation.isPending}
              disabled={Boolean(epgStatus?.running)}
            >
              Force EPG Refresh
            </Button>
            <Button size="xs" leftSection={<IconPlus size={14} />} onClick={handleAddClick}>
              Add Account
            </Button>
          </Group>
        )}
      </Flex>

      {accounts?.length === 0 ? (
        <Card shadow="sm" padding="xl" radius="md" withBorder>
          <Stack align="center" gap="md">
            <IconServer size={48} opacity={0.5} />
            <Text c="dimmed" ta="center">
              No accounts configured. Add an Xtream Codes account to get started.
            </Text>
            <Button onClick={handleAddClick}>Add Your First Account</Button>
          </Stack>
        </Card>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(min(330px, 100%), 1fr))', gap: 14 }}>
          {accounts?.map((account) => (
            <AccountCard
              key={account.id}
              account={account}
              isDefault={Number(appSettings?.default_account_id) === Number(account.id)}
              onSetDefault={handleSetDefault}
              onEdit={handleEdit}
              onDelete={handleDelete}
              onTest={handleTest}
              defaultPending={setDefaultAccountMutation.isPending}
              testPending={testMutation.isPending && testMutation.variables === account.id}
            />
          ))}
        </div>
      )}

      <Modal
        opened={modalOpened}
        onClose={closeModal}
        title={editingAccount ? 'Edit Account' : 'Add Account'}
        size="md"
      >
        <AccountForm
          account={editingAccount}
          onSubmit={handleSubmit}
          onCancel={closeModal}
          isLoading={createMutation.isPending || updateMutation.isPending}
        />
      </Modal>
    </Stack>
  )
}
