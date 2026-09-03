import { Fragment, useState, useEffect, useCallback, useContext, useMemo, useRef } from 'react'
import { UNSAFE_NavigationContext, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Title,
  Card,
  Group,
  Text,
  Stack,
  TextInput,
  Button,
  Code,
  Loader,
  Alert,
  Anchor,
  Badge,
  Switch,
  Select,
  SegmentedControl,
  SimpleGrid,
  MultiSelect,
  useMantineColorScheme,
  Modal,
  NavLink,
  Box,
  Paper,
  PasswordInput,
  Tooltip,
} from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  IconFolder,
  IconAlertCircle,
  IconAlertTriangle,
  IconCircleCheck,
  IconChevronLeft,
  IconChevronRight,
  IconMoon,
  IconPlus,
  IconTrash,
  IconKey,
  IconRefresh,
} from '@tabler/icons-react'
import dayjs from 'dayjs'

import { accountsApi, adminPlexApi, adminUsersApi, authApi, downloadsApi, epgApi, settingsApi } from '../api'
import AccountsSection from '../components/settings/AccountsSection'
import ComskipSection, { COMSKIP_DEFAULTS, getComskipErrors } from '../components/settings/ComskipSection'
import LogsSection from '../components/settings/LogsSection'
import NumberStepper from '../components/settings/NumberStepper'
import SaveBar from '../components/settings/SaveBar'
import SettingsSearch from '../components/settings/SettingsSearch'
import { SectionHeader, SettingRow, SubGroup } from '../components/settings/SettingsPrimitives'
import { SECTION_GROUPS, ADMIN_SECTIONS, DOWNLOAD_USER_SECTIONS } from '../components/settings/sections'
import classes from './Settings.module.css'

function isPlexNotConnectedError(message) {
  return (message || '').toLowerCase().includes('plex account is not connected')
}

const VAAPI_VENDOR_NAMES = {
  '0x8086': 'Intel',
  '0x1002': 'AMD',
  '0x10de': 'NVIDIA',
}

const VAAPI_KERNEL_LABELS = {
  amdgpu: 'AMD',
  radeon: 'AMD (legacy)',
  i915: 'Intel',
  xe: 'Intel (Xe)',
  nouveau: 'NVIDIA (open driver)',
  nvidia: 'NVIDIA',
}

// Builds the GPU picker options. A node we couldn't identify still gets listed —
// the user may know which card it is even when sysfs doesn't say.
function renderDeviceOptions(vaapi) {
  const devices = vaapi?.available_devices || []
  return devices.map((device) => {
    const shortName = device.device.split('/').pop()
    const vendorLabel =
      VAAPI_KERNEL_LABELS[(device.kernel_driver || '').toLowerCase()] ||
      VAAPI_VENDOR_NAMES[(device.vendor || '').toLowerCase()] ||
      null
    return {
      value: device.device,
      label: vendorLabel ? `${shortName} — ${vendorLabel}` : shortName,
    }
  })
}

// Returns a warning descriptor when VA-API has a problem worth surfacing,
// or null when everything is fine (working GPU setups stay silent).
function describeHardwareAccel(vaapi) {
  if (!vaapi || !vaapi.enabled) {
    return null
  }
  const vendorLabel =
    VAAPI_KERNEL_LABELS[(vaapi.kernel_driver || '').toLowerCase()] ||
    VAAPI_VENDOR_NAMES[(vaapi.vendor || '').toLowerCase()] ||
    null

  switch (vaapi.source) {
    case 'auto-detected':
    case 'env':
      return null
    case 'device-missing':
      return {
        color: 'gray',
        icon: <IconAlertCircle size={12} />,
        label: 'GPU encoding unavailable',
        detail:
          'No GPU is visible to the app. This is expected on macOS and Windows — Docker Desktop doesn\'t share the host GPU with containers. Recordings will be encoded on the CPU, which works fine but is slower.',
      }
    case 'sysfs-unavailable':
      return {
        color: 'yellow',
        icon: <IconAlertCircle size={12} />,
        label: 'GPU found, details unknown',
        detail:
          'A GPU device was found but the app couldn\'t identify it. Hardware acceleration may still work — try selecting VA-API and running a test transcode.',
      }
    case 'auto':
    default:
      return {
        color: 'yellow',
        icon: <IconAlertCircle size={12} />,
        label: vendorLabel ? `GPU found — ${vendorLabel}` : 'GPU found',
        detail: vendorLabel
          ? `Detected a ${vendorLabel} GPU but couldn\'t auto-pick a driver. Set LIBVA_DRIVER_NAME manually if encoding fails.`
          : 'Detected a GPU but couldn\'t identify a matching driver. Set LIBVA_DRIVER_NAME manually if encoding fails.',
      }
  }
}

// Sample values used to render the live filename previews client-side.
// Kept in line with the API's rendered_example values.
const TEMPLATE_SAMPLES = {
  tv_show: { show: 'Breaking Bad', season: 1, episode: 5, title: 'Gray Matter', date: '2024-01-14' },
  movie: { title: 'Inception', year: 2010 },
  sports: { title: 'FA Cup Final', date: '2024-01-14', channel: 'ESPN' },
  default: { channel: 'BBC One', title: 'Planet Earth', date: '2024-01-14' },
}

// Renders {name} and zero-padded {name:02d} placeholders; unknown
// placeholders are left as-is so typos stay visible in the preview.
function renderTemplateExample(template, sample) {
  return (template || '').replace(/\{(\w+)(?::0?(\d+)d)?\}/g, (match, name, pad) => {
    const value = sample[name]
    if (value === undefined) return match
    return pad ? String(value).padStart(Number(pad), '0') : String(value)
  })
}

// Renders a preview as a path: folder levels are tinted and separators dimmed,
// so a template containing "/" visibly produces directories.
function PreviewPath({ text, ext }) {
  const segments = text.split('/')
  return (
    <span>
      {segments.map((seg, i) => (
        <Fragment key={i}>
          {i > 0 && <span className={classes.templatePreviewSep}>/</span>}
          <span className={i < segments.length - 1 ? classes.templatePreviewDir : undefined}>{seg}</span>
        </Fragment>
      ))}
      <span className={classes.templatePreviewExt}>{ext}</span>
    </span>
  )
}

function TemplateBlock({ label, template, variables, sample, ext, onChange }) {
  const inputRef = useRef(null)

  function insertVariable(varName) {
    const insertion = `{${varName}}`
    const input = inputRef.current
    if (input) {
      const start = input.selectionStart ?? template.length
      const end = input.selectionEnd ?? template.length
      const newVal = template.slice(0, start) + insertion + template.slice(end)
      onChange(newVal)
      requestAnimationFrame(() => {
        input.focus()
        const pos = start + insertion.length
        input.setSelectionRange(pos, pos)
      })
    } else {
      onChange(template + insertion)
    }
  }

  return (
    <SubGroup label={label}>
      <TextInput
        ref={inputRef}
        aria-label={`${label} template`}
        value={template}
        onChange={(e) => onChange(e.target.value)}
        classNames={{ input: classes.monoInput }}
      />
      <Group gap={6} wrap="wrap" align="center">
        <Text size="xs" c="dimmed">Insert:</Text>
        {variables.map((v) => (
          <Code
            key={v.name}
            style={{ cursor: 'pointer' }}
            title={v.description}
            onClick={() => insertVariable(v.name)}
          >
            {`{${v.name}}`}
          </Code>
        ))}
      </Group>
      <div className={classes.templatePreview}>
        <span className={classes.templatePreviewArrow}>→</span>
        <PreviewPath text={renderTemplateExample(template, sample)} ext={ext} />
      </div>
    </SubGroup>
  )
}

function FolderStatusBadge({ status }) {
  if (!status) return null
  const ok = status.exists && status.writable
  const label = ok ? 'Writable' : status.exists ? 'Not writable' : 'Missing'
  const badge = (
    <Badge
      size="xs"
      color={ok ? 'green' : 'red'}
      variant="light"
      leftSection={ok ? <IconCircleCheck size={11} /> : <IconAlertTriangle size={11} />}
    >
      {label}
    </Badge>
  )
  if (!ok && status.error) {
    return (
      <Tooltip label={status.error} multiline w={280} events={{ hover: true, focus: true, touch: true }}>
        {badge}
      </Tooltip>
    )
  }
  return badge
}

function useBlocker(blocker, when = true) {
  const { navigator } = useContext(UNSAFE_NavigationContext)

  useEffect(() => {
    if (!when) return
    if (!navigator?.block) return undefined

    const unblock = navigator.block((tx) => {
      const autoUnblockingTx = {
        ...tx,
        retry() {
          unblock()
          tx.retry()
        },
      }
      blocker(autoUnblockingTx)
    })

    return unblock
  }, [navigator, blocker, when])
}

export default function Settings() {
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const [formData, setFormData] = useState(null)
  const [editedFields, setEditedFields] = useState(() => new Set())
  const [leaveModalOpen, setLeaveModalOpen] = useState(false)
  const [pendingNavigation, setPendingNavigation] = useState(null)
  const [isSavingAndLeaving, setIsSavingAndLeaving] = useState(false)
  const [activeSection, setActiveSection] = useState('security')
  const [mobileView, setMobileView] = useState('home')
  const [searchParams, setSearchParams] = useSearchParams()
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [newUserName, setNewUserName] = useState('')
  const [newUserDisplayName, setNewUserDisplayName] = useState('')
  const [newUserPassword, setNewUserPassword] = useState('')
  const [resetPasswordModalOpen, setResetPasswordModalOpen] = useState(false)
  const [resetTargetUser, setResetTargetUser] = useState(null)
  const [resetUserPassword, setResetUserPassword] = useState('')
  const [plexFormDirty, setPlexFormDirty] = useState(false)
  const [plexFormHydrated, setPlexFormHydrated] = useState(false)
  const [plexForm, setPlexForm] = useState({
    resource_id: '',
    resource_name: '',
    connection_uri: '',
    base_url: '',
    machine_identifier: '',
    library_section_ids: [],
    auto_allow_all_server_users: true,
    enabled: true,
  })
  const [plexResources, setPlexResources] = useState([])
  const [plexLibraries, setPlexLibraries] = useState([])
  const isMobile = useMediaQuery('(max-width: 820px)')
  const { colorScheme, setColorScheme } = useMantineColorScheme()
  const { data: authStatus, isLoading: authLoading } = useQuery({
    queryKey: ['auth', 'status'],
    queryFn: authApi.status,
  })
  const isAdmin = Boolean(authStatus?.is_admin)
  const isLocalDownloadUser = Boolean(!isAdmin && authStatus?.provider === 'local')
  const availableSections = isAdmin ? ADMIN_SECTIONS : DOWNLOAD_USER_SECTIONS

  useEffect(() => {
    if (authLoading) return
    const requestedSection = searchParams.get('section')
    if (!requestedSection) {
      setActiveSection(isAdmin ? 'recording' : 'security')
      setMobileView('home')
      return
    }
    const valid = availableSections.some((section) => section.id === requestedSection)
    const resolved = valid ? requestedSection : availableSections[0]?.id || 'security'
    setActiveSection(resolved)
    setMobileView(resolved)
  }, [authLoading, availableSections, isAdmin, searchParams])

  useEffect(() => {
    if (authLoading) return
    const valid = availableSections.some((section) => section.id === activeSection)
    if (!valid) {
      setActiveSection(availableSections[0]?.id || 'security')
    }
  }, [activeSection, authLoading, availableSections])

  const selectSection = useCallback((sectionId) => {
    const valid = availableSections.some((section) => section.id === sectionId)
    const resolved = valid ? sectionId : availableSections[0]?.id || 'security'
    setActiveSection(resolved)
    setMobileView(resolved)
    const next = new URLSearchParams(searchParams)
    next.set('section', resolved)
    setSearchParams(next, { replace: true })
    window.scrollTo({ top: 0 })
  }, [availableSections, searchParams, setSearchParams])

  const goMobileHome = useCallback(() => {
    setMobileView('home')
    const next = new URLSearchParams(searchParams)
    next.delete('section')
    setSearchParams(next, { replace: true })
    window.scrollTo({ top: 0 })
  }, [searchParams, setSearchParams])

  const { data: settings, isLoading, error } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
    enabled: isAdmin,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const { data: templateInfo } = useQuery({
    queryKey: ['settings', 'templates'],
    queryFn: settingsApi.getTemplates,
    enabled: isAdmin,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  const { data: toolsStatus } = useQuery({
    queryKey: ['settings', 'tools'],
    queryFn: settingsApi.getTools,
    enabled: isAdmin,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const { data: foldersStatus } = useQuery({
    queryKey: ['settings', 'folders-status'],
    queryFn: settingsApi.getFoldersStatus,
    enabled: isAdmin,
    refetchInterval: 60000,
  })
  const { data: diskSpace } = useQuery({
    queryKey: ['downloads', 'disk-space'],
    queryFn: downloadsApi.diskSpace,
    enabled: isAdmin,
    refetchInterval: 30000,
  })
  const { data: accountsList } = useQuery({
    queryKey: ['accounts'],
    queryFn: accountsApi.list,
    enabled: isAdmin,
  })
  const { data: epgStatus } = useQuery({
    queryKey: ['epg', 'status'],
    queryFn: epgApi.status,
    enabled: isAdmin,
    refetchInterval: 5000,
  })
  const epgRefreshMutation = useMutation({
    mutationFn: () => epgApi.refresh(false),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['epg', 'status'] })
      notifications.show({ title: 'Guide refresh started', message: 'The guide will update in the background.', color: 'green' })
    },
    onError: (err) => {
      notifications.show({ title: 'Refresh failed', message: err?.message || 'Could not start guide refresh.', color: 'red' })
    },
  })
  const { data: managedUsers = [] } = useQuery({
    queryKey: ['admin', 'users'],
    queryFn: adminUsersApi.list,
    enabled: isAdmin,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const { data: plexConfig } = useQuery({
    queryKey: ['admin', 'plex'],
    queryFn: adminPlexApi.get,
    enabled: isAdmin,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })

  // Orange dot on the Accounts rail item when a provider needs attention.
  const accountsNeedAttention = useMemo(() => {
    return (accountsList || []).some((account) => {
      if (account.last_connection_ok === false) return true
      if (!account.expiration_date) return false
      return dayjs(account.expiration_date).diff(dayjs(), 'day') <= 7
    })
  }, [accountsList])

  useEffect(() => {
    if (!plexConfig) return
    if (plexFormHydrated && plexFormDirty) return
    setPlexForm((prev) => ({
      ...prev,
      resource_id: plexConfig.resource_id || '',
      resource_name: plexConfig.resource_name || '',
      connection_uri: plexConfig.connection_uri || plexConfig.base_url || '',
      base_url: plexConfig.base_url || '',
      machine_identifier: plexConfig.machine_identifier || '',
      library_section_ids: plexConfig.library_section_ids || [],
      auto_allow_all_server_users: Boolean(plexConfig.auto_allow_all_server_users),
      enabled: Boolean(plexConfig.enabled),
    }))
    setPlexFormHydrated(true)
    setPlexFormDirty(false)
  }, [plexConfig, plexFormDirty, plexFormHydrated])

  // Hydrate the form on first load; afterwards keep unedited fields in sync
  // with the server (e.g. default account changed from the Accounts section).
  useEffect(() => {
    if (!settings) return
    setFormData((prev) => {
      if (!prev) return { ...settings }
      const next = { ...settings }
      editedFields.forEach((field) => {
        next[field] = prev[field]
      })
      return next
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings])

  // Dirty tracking: fields the user touched whose value differs from the
  // last-saved snapshot. Drives the save bar count and the save payload.
  const changedFields = useMemo(() => {
    if (!settings || !formData) return []
    return [...editedFields].filter(
      (field) => JSON.stringify(formData[field] ?? null) !== JSON.stringify(settings[field] ?? null)
    )
  }, [settings, formData, editedFields])
  const changedCount = changedFields.length
  const hasChanges = changedCount > 0

  const blockNavigation = useCallback((tx) => {
    if (!hasChanges) {
      tx.retry()
      return
    }
    setPendingNavigation(tx)
    setLeaveModalOpen(true)
  }, [hasChanges])
  useBlocker(blockNavigation, hasChanges)

  const updateMutation = useMutation({
    mutationFn: settingsApi.update,
    onSuccess: (data) => {
      queryClient.setQueryData(['settings'], data)
      queryClient.invalidateQueries({ queryKey: ['settings', 'folders-status'] })
      setFormData({ ...data })
      setEditedFields(new Set())
      notifications.show({
        title: 'Settings Saved',
        message: 'Your settings have been updated',
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

  const changePasswordMutation = useMutation({
    mutationFn: ({ current, next }) => authApi.changePassword(current, next),
    onSuccess: async () => {
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      queryClient.setQueryData(['auth', 'status'], (previous) => {
        if (!previous) return previous
        return {
          ...previous,
          password_reset_required: false,
        }
      })
      await queryClient.invalidateQueries({ queryKey: ['auth', 'status'] })
      notifications.show({
        title: 'Password Updated',
        message: isAdmin ? 'Admin password has been changed.' : 'Your password has been changed.',
        color: 'green',
      })
      if (!isAdmin) {
        navigate('/browse', { replace: true })
      }
    },
    onError: (error) => {
      notifications.show({
        title: 'Password Change Failed',
        message: error.message,
        color: 'red',
      })
    },
  })
  const createUserMutation = useMutation({
    mutationFn: adminUsersApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      setNewUserName('')
      setNewUserDisplayName('')
      setNewUserPassword('')
      notifications.show({ title: 'User Added', message: 'Download-only user created.', color: 'green' })
    },
    onError: (error) => {
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })
  const deleteUserMutation = useMutation({
    mutationFn: adminUsersApi.delete,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      notifications.show({ title: 'User Removed', message: 'User deleted.', color: 'green' })
    },
    onError: (error) => {
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })
  const resetUserPasswordMutation = useMutation({
    mutationFn: ({ userId, password }) => adminUsersApi.update(userId, { password }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] })
      notifications.show({
        title: 'Password Updated',
        message: 'User password updated. They will be required to reset it after login.',
        color: 'green',
      })
      setResetPasswordModalOpen(false)
      setResetTargetUser(null)
      setResetUserPassword('')
    },
    onError: (error) => {
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })
  const savePlexMutation = useMutation({
    mutationFn: adminPlexApi.save,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'plex'] })
      setPlexFormDirty(false)
      notifications.show({ title: 'Plex Saved', message: 'Plex server settings updated.', color: 'green' })
    },
    onError: (error) => {
      notifications.show({ title: 'Error', message: error.message, color: 'red' })
    },
  })
  const disconnectPlexMutation = useMutation({
    mutationFn: adminPlexApi.disconnect,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'plex'] })
      setPlexResources([])
      setPlexLibraries([])
      setPlexForm((prev) => ({
        ...prev,
        resource_id: '',
        resource_name: '',
        connection_uri: '',
        base_url: '',
        machine_identifier: '',
        library_section_ids: [],
        auto_allow_all_server_users: true,
        enabled: true,
      }))
      notifications.show({ title: 'Plex Disconnected', message: 'Plex integration has been disconnected.', color: 'green' })
    },
    onError: (error) => {
      notifications.show({ title: 'Disconnect Failed', message: error.message, color: 'red' })
    },
  })
  const listPlexResourcesMutation = useMutation({
    mutationFn: adminPlexApi.listResources,
    onSuccess: (resources) => {
      setPlexResources(resources || [])
    },
    onError: (error) => {
      if (isPlexNotConnectedError(error.message)) {
        notifications.show({
          title: 'Connect Plex First',
          message: 'Connect a Plex account to load available servers.',
          color: 'yellow',
        })
        return
      }
      notifications.show({ title: 'Plex Resources Failed', message: error.message, color: 'red' })
    },
  })
  const listPlexLibrariesMutation = useMutation({
    mutationFn: ({ resourceId, connectionUri }) =>
      adminPlexApi.listLibraries(resourceId, {
        connection_uri: connectionUri || null,
      }),
    onSuccess: (data) => {
      setPlexLibraries(data?.libraries || [])
      if (data?.base_url) {
        updatePlexForm((prev) => ({
          ...prev,
          connection_uri: data.connection_uri || data.base_url,
          base_url: data.base_url,
        }))
      }
    },
    onError: (error) => {
      notifications.show({ title: 'Plex Libraries Failed', message: error.message, color: 'red' })
    },
  })
  const connectPlexStartMutation = useMutation({
    mutationFn: () => adminPlexApi.connectStart(),
    onSuccess: (data, variables) => {
      const popup = variables?.popupRef || null
      if (popup && !popup.closed) {
        try {
          popup.opener = null
          popup.location.replace(data.auth_url)
        } catch {
          // noop
        }
      }
      const pollMs = Math.max(1, Number(data.poll_interval_seconds || 2)) * 1000
      const timeoutMs = Math.max(30, Number(data.expires_in || 300)) * 1000
      const startedAt = Date.now()
      const pinId = data.id
      const timer = setInterval(async () => {
        if (Date.now() - startedAt > timeoutMs) {
          clearInterval(timer)
          notifications.show({ title: 'Plex Connection Timeout', message: 'Please try again.', color: 'yellow' })
          return
        }
        try {
          const status = await adminPlexApi.connectComplete(pinId)
          if (status?.status !== 'connected') {
            return
          }
          clearInterval(timer)
          try {
            if (popup && !popup.closed) popup.close()
          } catch {
            // noop
          }
          notifications.show({ title: 'Plex Connected', message: 'Choose a server to continue.', color: 'green' })
          listPlexResourcesMutation.mutate()
        } catch (error) {
          if ((error.message || '').includes('not complete')) {
            return
          }
          clearInterval(timer)
          notifications.show({ title: 'Plex Connection Failed', message: error.message, color: 'red' })
        }
      }, pollMs)
    },
    onError: (error) => {
      notifications.show({ title: 'Plex Connection Failed', message: error.message, color: 'red' })
    },
  })

  const updatePlexForm = (updater) => {
    setPlexForm((prev) => (typeof updater === 'function' ? updater(prev) : { ...prev, ...updater }))
    setPlexFormDirty(true)
  }

  useEffect(() => {
    if (activeSection !== 'plex') return
    if (!plexConfig?.token_configured) return
    if (plexResources.length > 0) return
    if (listPlexResourcesMutation.isPending) return
    listPlexResourcesMutation.mutate()
  }, [activeSection, listPlexResourcesMutation.isPending, plexConfig?.token_configured, plexResources.length])

  useEffect(() => {
    if (!plexConfig?.token_configured) return
    if (!plexForm.resource_id || activeSection !== 'plex') return
    if (plexLibraries.length > 0) return
    listPlexLibrariesMutation.mutate({
      resourceId: plexForm.resource_id,
      connectionUri: plexForm.connection_uri || plexForm.base_url,
    })
  }, [activeSection, plexConfig?.token_configured, plexForm.base_url, plexForm.connection_uri, plexForm.resource_id, plexLibraries.length])

  const handleChangePassword = (e) => {
    e.preventDefault()
    if (!currentPassword || !newPassword) {
      notifications.show({
        title: 'Missing fields',
        message: 'Enter current and new password.',
        color: 'yellow',
      })
      return
    }
    if (newPassword.length < 8) {
      notifications.show({
        title: 'Password too short',
        message: 'New password must be at least 8 characters.',
        color: 'yellow',
      })
      return
    }
    if (newPassword !== confirmPassword) {
      notifications.show({
        title: 'Passwords do not match',
        message: 'New password and confirmation must be the same.',
        color: 'yellow',
      })
      return
    }
    changePasswordMutation.mutate({ current: currentPassword, next: newPassword })
  }

  const applyChanges = (patch) => {
    setFormData((prev) => ({ ...prev, ...patch }))
    setEditedFields((prev) => new Set([...prev, ...Object.keys(patch)]))
  }

  const handleChange = (field, value) => {
    applyChanges({ [field]: value })
  }

  const buildSavePayload = () => {
    const payload = {}
    changedFields.forEach((field) => {
      payload[field] = formData[field]
    })
    return payload
  }

  const handleSave = () => {
    updateMutation.mutate(buildSavePayload())
  }

  const handleReset = () => {
    setFormData({ ...settings })
    setEditedFields(new Set())
  }

  useEffect(() => {
    if (!hasChanges) {
      setLeaveModalOpen(false)
      setPendingNavigation(null)
    }
  }, [hasChanges])

  useEffect(() => {
    const handleBeforeUnload = (event) => {
      if (!hasChanges) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [hasChanges])

  const handleStay = () => {
    setLeaveModalOpen(false)
    setPendingNavigation(null)
  }

  const handleDiscardAndContinue = () => {
    const tx = pendingNavigation
    setLeaveModalOpen(false)
    setPendingNavigation(null)
    handleReset()
    tx?.retry?.()
  }

  const handleSaveAndContinue = async () => {
    const tx = pendingNavigation
    if (!tx) {
      return
    }
    setIsSavingAndLeaving(true)
    try {
      await updateMutation.mutateAsync(buildSavePayload())
      setLeaveModalOpen(false)
      setPendingNavigation(null)
      tx.retry()
    } catch (error) {
      setIsSavingAndLeaving(false)
    }
    setIsSavingAndLeaving(false)
  }

  if (authLoading || (isAdmin && isLoading)) {
    return (
      <Stack align="center" justify="center" h={300}>
        <Loader />
      </Stack>
    )
  }

  if (isAdmin && error) {
    return (
      <Alert icon={<IconAlertCircle size={16} />} title="Error" color="red">
        Failed to load settings: {error.message}
      </Alert>
    )
  }

  if (isAdmin && !formData) {
    return null
  }

  const compactButtonSize = isMobile ? 'sm' : 'xs'
  const switchSize = isMobile ? 'lg' : undefined

  const renderRecording = () => (
    <Stack gap="xl">
      <SectionHeader title="Recording" description="Folders, concurrency, and default timing for recordings" />

      <SubGroup label="Storage">
        <TextInput
          label={
            <Group component="span" gap={8} display="inline-flex" align="center">
              <span>Download folder</span>
              <FolderStatusBadge status={foldersStatus?.download_folder} />
            </Group>
          }
          description="Where files are saved while downloading"
          value={formData.download_folder}
          onChange={(e) => handleChange('download_folder', e.target.value)}
          leftSection={<IconFolder size={16} />}
          classNames={{ input: classes.monoInput }}
          title={formData.download_folder}
        />
        <TextInput
          label={
            <Group component="span" gap={8} display="inline-flex" align="center">
              <span>Completed folder</span>
              <FolderStatusBadge status={foldersStatus?.completed_folder} />
            </Group>
          }
          description="Where finished files are moved after processing"
          value={formData.completed_folder}
          onChange={(e) => handleChange('completed_folder', e.target.value)}
          leftSection={<IconFolder size={16} />}
          classNames={{ input: classes.monoInput }}
          title={formData.completed_folder}
        />
        {diskSpace?.available && diskSpace.disk_free_gb != null && (
          <Group>
            <Badge variant="outline" color={diskSpace.is_low ? 'red' : 'gray'} tt="none" fw={500}>
              {diskSpace.disk_free_gb} GB free on recording drive
            </Badge>
          </Group>
        )}
      </SubGroup>

      <SubGroup label="Limits">
        <Stack gap={0}>
          <SettingRow label="Max concurrent downloads" description="How many downloads can run simultaneously">
            <NumberStepper
              aria-label="Max concurrent downloads"
              min={1}
              max={5}
              value={formData.max_concurrent_downloads}
              onChange={(val) => handleChange('max_concurrent_downloads', val)}
            />
          </SettingRow>
          <SettingRow label="Minimum free space" description="Pause scheduled recordings if free disk space drops below this">
            <NumberStepper
              aria-label="Minimum free space"
              min={1}
              max={10000}
              unit="GB"
              value={formData.min_free_space_gb}
              onChange={(val) => handleChange('min_free_space_gb', val)}
            />
          </SettingRow>
        </Stack>
      </SubGroup>

      <SubGroup label="Padding defaults">
        <Stack gap={0}>
          <SettingRow label="Start early" description="Begin recordings this many minutes before the scheduled start">
            <NumberStepper
              aria-label="Start early"
              min={0}
              max={120}
              unit="min"
              value={formData.default_pre_padding_minutes}
              onChange={(val) => handleChange('default_pre_padding_minutes', val)}
            />
          </SettingRow>
          <SettingRow label="End late" description="Keep recording this many minutes past the scheduled end">
            <NumberStepper
              aria-label="End late"
              min={0}
              max={120}
              unit="min"
              value={formData.default_post_padding_minutes}
              onChange={(val) => handleChange('default_post_padding_minutes', val)}
            />
          </SettingRow>
        </Stack>
      </SubGroup>
    </Stack>
  )

  const renderProcessing = () => {
    const container = !formData.transcode_enabled ? 'ts' : formData.transcode_format === 'mp4' ? 'mp4' : 'mkv'
    const method = formData.remux_only ? 'remux' : 'encode'
    const isTs = container === 'ts'
    const comskipOn = Boolean(formData.comskip_enabled)
    // Off / Mark / Cut. comskip_cut defaults to Cut (true) when comskip is on.
    const comskipMode = !comskipOn ? 'off' : (formData.comskip_cut === false ? 'mark' : 'cut')
    const ffmpegReady = toolsStatus?.ffmpeg?.available
    const comskipReady = toolsStatus?.comskip?.available

    const summary = isTs
      ? 'No processing. The raw .ts file is kept as downloaded.'
      : method === 'remux'
        ? `The stream is wrapped in an ${container.toUpperCase()} file without re-encoding. Fast and lossless.`
        : container === 'mp4'
          ? 'Re-encode to MP4 using ffmpeg. Best compatibility with most devices and media players.'
          : 'Re-encode to MKV using ffmpeg. Slower but fixes compatibility issues.'

    const handleContainerChange = (value) => {
      if (value === 'ts') {
        // Cut needs a container, so switching to Keep .ts drops Cut. Mark works
        // on .ts (sidecar only), so it is left in place.
        const patch = { transcode_enabled: false }
        if (comskipMode === 'cut') patch.comskip_enabled = false
        applyChanges(patch)
      } else {
        applyChanges({ transcode_enabled: true, transcode_format: value })
      }
    }

    const handleCommercialsChange = (mode) => {
      if (mode === 'off') {
        applyChanges({ comskip_enabled: false })
      } else if (mode === 'mark') {
        // Mark detects but does not cut; it never forces a re-encode.
        applyChanges({ comskip_enabled: true, comskip_cut: false })
      } else {
        // Cut physically removes commercials and needs a container conversion.
        const patch = { comskip_enabled: true, comskip_cut: true }
        if (isTs) {
          patch.transcode_enabled = true
          patch.transcode_format = 'mkv'
        }
        applyChanges(patch)
      }
    }

    return (
      <Stack gap="xl">
        <SectionHeader title="Post-Processing" description="What happens to a recording after the download finishes" />

        <SubGroup label="Output format">
          <Stack gap={0}>
            <SettingRow label="Container" description={summary}>
              <SegmentedControl
                value={container}
                onChange={handleContainerChange}
                data={[
                  { value: 'ts', label: 'Keep .ts' },
                  { value: 'mkv', label: 'MKV', disabled: ffmpegReady === false },
                  { value: 'mp4', label: 'MP4', disabled: ffmpegReady === false },
                ]}
              />
            </SettingRow>
            {!isTs && (
              <SettingRow
                label="Conversion method"
                description={method === 'remux'
                  ? 'Repackage only — fast and lossless'
                  : 'Full re-encode with ffmpeg — slower, smaller files'}
              >
                <SegmentedControl
                  value={method}
                  onChange={(value) => handleChange('remux_only', value === 'remux')}
                  data={[
                    { value: 'remux', label: 'Remux (fast)' },
                    { value: 'encode', label: 'Re-encode' },
                  ]}
                />
              </SettingRow>
            )}
            <SettingRow
              label="Commercials"
              description={isTs
                ? 'Mark writes an .edl sidecar (no cut). Cutting needs MKV or MP4 above.'
                : 'Mark keeps the full video — it writes an .edl sidecar and embeds Plex chapters at the ad breaks; Cut removes them.'}
            >
              {comskipOn && (
                <Button variant="subtle" size="xs" onClick={() => selectSection('comskip')}>
                  Tune detection →
                </Button>
              )}
              <SegmentedControl
                value={comskipMode}
                onChange={handleCommercialsChange}
                data={[
                  { value: 'off', label: 'Off' },
                  { value: 'mark', label: 'Mark only' },
                  { value: 'cut', label: 'Cut out', disabled: isTs },
                ]}
              />
            </SettingRow>
          </Stack>

          {comskipMode === 'mark' && isTs && (
            <Alert color="yellow" variant="light">
              <Text size="sm">
                Plex chapter-skip needs MKV or MP4. On{' '}
                <Text component="span" fw={500}>Keep .ts</Text> you'll only get the .edl sidecar
                (Kodi/Jellyfin/Emby still work).
              </Text>
            </Alert>
          )}

          {ffmpegReady === false && !isTs && (
            <Alert color="yellow" variant="light">
              <Text size="sm">
                ffmpeg is unavailable. The Docker image includes ffmpeg; install or fix it manually if running locally.
                {' '}To save recordings without converting, switch the container above to{' '}
                <Text component="span" fw={500}>Keep .ts</Text>.
              </Text>
              {toolsStatus?.ffmpeg?.error && (
                <Text size="xs" c="dimmed" mt={4}>{toolsStatus.ffmpeg.error}</Text>
              )}
            </Alert>
          )}

          {comskipReady === false && comskipOn && (
            <Alert color="yellow" variant="light">
              <Text size="sm">
                Comskip binary not found in the default PATH. Enter the path to your comskip binary below, or see{' '}
                <a href="https://github.com/erikkaashoek/Comskip" target="_blank" rel="noopener noreferrer">
                  github.com/erikkaashoek/Comskip
                </a>{' '}
                for installation instructions.
              </Text>
              {toolsStatus?.comskip?.error && (
                <Text size="xs" c="dimmed" mt={4}>{toolsStatus.comskip.error}</Text>
              )}
            </Alert>
          )}
        </SubGroup>

        {!isTs && method === 'encode' && (
          <SubGroup label="Encoding">
            <Stack gap={0}>
              <SettingRow label="Hardware acceleration" description="Use the GPU for faster encoding">
                <SegmentedControl
                  value={formData.hw_accel || 'cpu'}
                  onChange={(val) => handleChange('hw_accel', val)}
                  data={toolsStatus?.hardware_accels?.map((hw) => ({
                    value: hw.id,
                    label: hw.name,
                    disabled: !hw.available,
                  })) || [{ value: 'cpu', label: 'CPU (Software)' }]}
                />
              </SettingRow>
            </Stack>
            {formData.hw_accel === 'vaapi' && renderDeviceOptions(toolsStatus?.vaapi).length > 0 && (
              <Stack gap={0}>
                <SettingRow
                  label="GPU device"
                  description={
                    toolsStatus?.vaapi?.device_locked_by_env
                      ? 'Pinned by CATCHUP_VAAPI_RENDER_DEVICE; unset it to choose here'
                      : 'Which GPU to encode on, when the system has more than one'
                  }
                >
                  <Select
                    aria-label="GPU device"
                    disabled={toolsStatus?.vaapi?.device_locked_by_env}
                    value={formData.vaapi_render_device || ''}
                    onChange={(val) => handleChange('vaapi_render_device', val || '')}
                    data={[
                      { value: '', label: 'Automatic (first available)' },
                      ...renderDeviceOptions(toolsStatus?.vaapi),
                    ]}
                    allowDeselect={false}
                  />
                </SettingRow>
              </Stack>
            )}
            {toolsStatus?.vaapi?.enabled && formData.hw_accel === 'vaapi' && (() => {
              const hwStatus = describeHardwareAccel(toolsStatus.vaapi)
              if (!hwStatus) return null
              return (
                <Alert color={hwStatus.color === 'gray' ? 'blue' : 'yellow'} variant="light" icon={hwStatus.icon}>
                  <Text size="sm" fw={500}>{hwStatus.label}</Text>
                  <Text size="xs" c="dimmed" mt={4}>{hwStatus.detail}</Text>
                </Alert>
              )
            })()}
          </SubGroup>
        )}

        <SubGroup label="Pipeline">
          <Stack gap={0}>
            <SettingRow
              label="Max concurrent post-processing"
              description="How many files can be processed at once — 1 is recommended"
            >
              <NumberStepper
                aria-label="Max concurrent post-processing"
                min={1}
                max={10}
                value={formData.max_concurrent_post_processing ?? 1}
                onChange={(val) => handleChange('max_concurrent_post_processing', val)}
              />
            </SettingRow>
            {!isTs && (
              <SettingRow
                label="Delete original after processing"
                description="Remove the source .ts file once processing is complete"
              >
                <Switch
                  size={switchSize}
                  aria-label="Delete original after processing"
                  checked={formData.delete_original_after_transcode !== false}
                  onChange={(e) => handleChange('delete_original_after_transcode', e.currentTarget.checked)}
                />
              </SettingRow>
            )}
            <SettingRow
              label="Write .nfo files"
              description="Save a Kodi-style .nfo next to each finished recording so Plex and Jellyfin match it to the right show"
            >
              <Switch
                size={switchSize}
                aria-label="Write .nfo files"
                checked={formData.write_nfo_files !== false}
                onChange={(e) => handleChange('write_nfo_files', e.currentTarget.checked)}
              />
            </SettingRow>
          </Stack>

          {(formData.max_concurrent_post_processing ?? 1) > 1 && (
            <Alert icon={<IconAlertCircle size={16} />} color="yellow" variant="light">
              <Text size="sm">
                Running multiple conversions at once can saturate your CPU/GPU and disk. Some GPUs also have limited
                concurrent encoding sessions.
              </Text>
            </Alert>
          )}

          {(comskipOn || comskipReady) && (
            <TextInput
              label="Comskip binary path (optional)"
              description="Custom path to the comskip binary"
              placeholder="/usr/local/bin/comskip"
              value={formData.comskip_path || ''}
              onChange={(e) => handleChange('comskip_path', e.target.value || null)}
              classNames={{ input: classes.monoInput }}
            />
          )}
        </SubGroup>
      </Stack>
    )
  }

  const renderNaming = () => {
    const ext = formData.transcode_enabled ? `.${formData.transcode_format || 'mkv'}` : '.ts'
    return (
      <Stack gap="xl">
        <SectionHeader title="File Naming" description="How downloaded files are named for your media library" />

        <Alert color="blue" variant="light" icon={<IconAlertCircle size={16} />}>
          The file extension ({ext}, from your Post-Processing format) is added automatically — don&apos;t include
          one in templates. Use <Code>/</Code> to sort recordings into folders, for example{' '}
          <Code>{'{show}/Season {season:02d}/{title}'}</Code>. Folders are created under your download folder.
          Slashes inside a show or episode name (like &quot;AC/DC&quot;) become a dash instead, so they never
          make a folder.
        </Alert>

        {templateInfo?.tv_show && (
          <TemplateBlock
            label="TV shows"
            template={formData.tv_template}
            variables={templateInfo.tv_show.variables}
            sample={TEMPLATE_SAMPLES.tv_show}
            ext={ext}
            onChange={(val) => handleChange('tv_template', val)}
          />
        )}
        {templateInfo?.movie && (
          <TemplateBlock
            label="Movies"
            template={formData.movie_template}
            variables={templateInfo.movie.variables}
            sample={TEMPLATE_SAMPLES.movie}
            ext={ext}
            onChange={(val) => handleChange('movie_template', val)}
          />
        )}
        {templateInfo?.sports && (
          <TemplateBlock
            label="Sports"
            template={formData.sports_template}
            variables={templateInfo.sports.variables}
            sample={TEMPLATE_SAMPLES.sports}
            ext={ext}
            onChange={(val) => handleChange('sports_template', val)}
          />
        )}
        {templateInfo?.default && (
          <TemplateBlock
            label="Everything else"
            template={formData.default_template}
            variables={templateInfo.default.variables}
            sample={TEMPLATE_SAMPLES.default}
            ext={ext}
            onChange={(val) => handleChange('default_template', val)}
          />
        )}
      </Stack>
    )
  }

  const renderGuide = () => {
    const lastSync = epgStatus?.last_completed_at
    const lastError = epgStatus?.last_error
    let lastSyncLabel = 'Guide not yet synced'
    if (lastSync) {
      const d = new Date(lastSync)
      const now = new Date()
      const isToday = d.toDateString() === now.toDateString()
      const timeStr = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      lastSyncLabel = isToday
        ? `Today at ${timeStr}`
        : `${d.toLocaleDateString([], { month: 'long', day: 'numeric' })} at ${timeStr}`
    }
    const isRunning = epgStatus?.running || epgRefreshMutation.isPending
    const intervalHours = epgStatus?.interval_hours
    return (
      <Stack gap="xl">
        <SectionHeader title="Program Guide" description="EPG sync status and refresh schedule" />

        <SubGroup label="Sync">
          <Stack gap={0}>
            <SettingRow
              label={lastError ? 'Last sync attempt' : 'Last synced'}
              description="When Mustarrd last updated the program guide from your provider"
            >
              <Text size="sm" c={lastSync ? undefined : 'dimmed'}>{lastSyncLabel}</Text>
            </SettingRow>
            {intervalHours != null && (
              <SettingRow
                label="Automatic refresh"
                description="How often the guide updates in the background — set with the CATCHUP_EPG_REFRESH_INTERVAL_HOURS environment variable"
              >
                <Text size="sm">Every {intervalHours} hour{Number(intervalHours) === 1 ? '' : 's'}</Text>
              </SettingRow>
            )}
            <SettingRow
              label="Refresh now"
              description="Fetch the latest program guide data from your provider in the background"
            >
              <Button
                size={compactButtonSize}
                variant="light"
                leftSection={<IconRefresh size={14} />}
                loading={isRunning}
                disabled={isRunning}
                onClick={() => epgRefreshMutation.mutate()}
              >
                {isRunning ? 'Refreshing…' : 'Refresh Now'}
              </Button>
            </SettingRow>
          </Stack>
          {lastError && (
            <Alert color="orange" variant="light" icon={<IconAlertCircle size={16} />}>
              Guide sync failed. Check your provider connection in{' '}
              <Anchor c="orange.5" onClick={() => selectSection('accounts')}>Accounts</Anchor>.
            </Alert>
          )}
        </SubGroup>
      </Stack>
    )
  }

  const renderAppearance = () => (
    <Stack gap="xl">
      <SectionHeader title="Appearance" description="Theme and display preferences" />

      <Stack gap={0}>
        <SettingRow label="Theme" description="Mustarrd was born for the dark, but you do you">
          <SegmentedControl
            value={colorScheme === 'light' ? 'light' : 'dark'}
            onChange={(value) => setColorScheme(value)}
            data={[
              {
                value: 'dark',
                label: (
                  <Group component="span" gap={6} wrap="nowrap" display="inline-flex" align="center">
                    <IconMoon size={14} />
                    Dark
                  </Group>
                ),
              },
              { value: 'light', label: 'Light' },
            ]}
          />
        </SettingRow>
      </Stack>
    </Stack>
  )

  const renderSecurity = () => (
    <Stack gap="xl">
      <SectionHeader
        title="Security"
        description={isAdmin
          ? 'Change your admin account password'
          : 'Change your local download user password'}
      />

      {!isAdmin && !isLocalDownloadUser && (
        <Alert color="yellow" variant="light">
          <Text size="sm">
            Password changes are only available for local accounts. This account signs in through Plex.
          </Text>
        </Alert>
      )}

      <form onSubmit={handleChangePassword} style={{ maxWidth: 420 }}>
        <Stack gap="sm">
          <PasswordInput
            label={isAdmin ? 'Current Admin Password' : 'Current Password'}
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            autoComplete="current-password"
          />
          <PasswordInput
            label={isAdmin ? 'New Admin Password' : 'New Password'}
            description="Minimum 8 characters"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
          />
          <PasswordInput
            label={isAdmin ? 'Confirm New Admin Password' : 'Confirm New Password'}
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            autoComplete="new-password"
            error={confirmPassword && confirmPassword !== newPassword ? 'Passwords do not match' : null}
          />
          <Group justify="flex-start">
            <Button
              type="submit"
              loading={changePasswordMutation.isPending}
              disabled={
                (!isAdmin && !isLocalDownloadUser) ||
                !currentPassword ||
                newPassword.length < 8 ||
                newPassword !== confirmPassword
              }
            >
              Change Password
            </Button>
          </Group>
        </Stack>
      </form>
    </Stack>
  )

  const renderUsers = () => (
    <Stack gap="xl">
      <SectionHeader
        title="Users"
        description="Download-only users can browse, schedule, and download — but can't change settings"
      />

      <SubGroup label="Members">
        <div>
          {managedUsers.map((user) => {
            const displayName = user.display_name || user.username || `User ${user.id}`
            const isPlexUser = !user.username
            return (
              <div key={user.id} className={classes.userRow}>
                <span className={classes.userAvatar}>{displayName.slice(0, 1).toUpperCase()}</span>
                <Stack gap={0} style={{ flex: 1, minWidth: 0 }}>
                  <Text size="sm" fw={500}>{displayName}</Text>
                  <Text size="xs" c="dimmed">
                    {isPlexUser ? 'Signs in with Plex' : `@${user.username}`}
                    {user.password_reset_required ? ' · must reset password at next login' : ''}
                  </Text>
                </Stack>
                <Badge variant="outline" color={isPlexUser ? 'gray' : 'yellow'}>
                  {isPlexUser ? 'Plex' : 'Local'}
                </Badge>
                {user.username && (
                  <Button
                    variant="subtle"
                    size={compactButtonSize}
                    leftSection={<IconKey size={14} />}
                    onClick={() => {
                      setResetTargetUser(user)
                      setResetUserPassword('')
                      setResetPasswordModalOpen(true)
                    }}
                  >
                    Set Password
                  </Button>
                )}
                <Button
                  variant="subtle"
                  color="red"
                  size={compactButtonSize}
                  px={8}
                  aria-label={`Delete ${displayName}`}
                  onClick={() => deleteUserMutation.mutate(user.id)}
                  loading={deleteUserMutation.isPending}
                >
                  <IconTrash size={14} />
                </Button>
              </div>
            )
          })}
          {managedUsers.length === 0 && (
            <Text size="sm" c="dimmed">No download-only users yet.</Text>
          )}
        </div>
      </SubGroup>

      <SubGroup label="Add a download-only user">
        <div className={classes.userAddGrid}>
          <TextInput
            label="Username"
            placeholder="john"
            value={newUserName}
            onChange={(e) => setNewUserName(e.target.value)}
          />
          <TextInput
            label="Display name"
            placeholder="John"
            value={newUserDisplayName}
            onChange={(e) => setNewUserDisplayName(e.target.value)}
          />
          <PasswordInput
            label="Password"
            placeholder="Min 8 chars"
            value={newUserPassword}
            onChange={(e) => setNewUserPassword(e.target.value)}
          />
          <Button
            leftSection={<IconPlus size={14} />}
            onClick={() =>
              createUserMutation.mutate({
                username: newUserName,
                display_name: newUserDisplayName || null,
                password: newUserPassword || null,
                status: 'active',
              })
            }
            disabled={!newUserName}
            loading={createUserMutation.isPending}
          >
            Add
          </Button>
        </div>
        <Alert color="blue" variant="light" icon={<IconAlertCircle size={15} />}>
          Plex sign-in is managed in{' '}
          <Anchor onClick={() => selectSection('plex')}>Plex Integration</Anchor>.
        </Alert>
      </SubGroup>
    </Stack>
  )

  const renderPlex = () => (
    <Stack gap="xl">
      <SectionHeader
        title="Plex Integration"
        description="Plex-based sign-in for your users, and automatic library scans when recordings complete"
      />

      <SubGroup label="Account">
        {!plexConfig?.token_configured ? (
          <>
            <Group>
              <Button
                onClick={() => {
                  const popup = window.open('about:blank', '_blank')
                  connectPlexStartMutation.mutate({ popupRef: popup })
                }}
                loading={connectPlexStartMutation.isPending}
              >
                Connect Plex Account
              </Button>
            </Group>
            <Alert color="blue" variant="light" icon={<IconAlertCircle size={16} />}>
              No Plex account connected. Click Connect Plex Account to sign in with Plex. Your servers and
              libraries will appear here once connected.
            </Alert>
          </>
        ) : (
          <Stack gap={0}>
            <SettingRow label="Plex account" description="Used for Plex sign-in and library refreshes">
              <Badge color="green" variant="light" leftSection={<IconCircleCheck size={11} />}>
                Connected
              </Badge>
              <Button
                variant="subtle"
                size={compactButtonSize}
                onClick={() => listPlexResourcesMutation.mutate()}
                loading={listPlexResourcesMutation.isPending}
              >
                Refresh Servers
              </Button>
              <Button
                variant="subtle"
                color="red"
                size={compactButtonSize}
                onClick={() => disconnectPlexMutation.mutate()}
                loading={disconnectPlexMutation.isPending}
              >
                Disconnect
              </Button>
            </SettingRow>
          </Stack>
        )}
      </SubGroup>

      {plexConfig?.token_configured && (
        <>
          <SubGroup label="Server">
            <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
              <Select
                label="Plex server"
                placeholder="Select an owned server"
                data={plexResources.map((r) => ({
                  value: r.resource_id,
                  label: `${r.name}${r.platform ? ` (${r.platform})` : ''}`,
                }))}
                value={plexForm.resource_id || null}
                onChange={(value) => {
                  if (!value) return
                  const selected = plexResources.find((r) => r.resource_id === value)
                  updatePlexForm((prev) => ({
                    ...prev,
                    resource_id: value,
                    resource_name: selected?.name || '',
                    machine_identifier: selected?.machine_identifier || null,
                    connection_uri: selected?.base_url || '',
                    base_url: selected?.base_url || '',
                  }))
                  setPlexLibraries([])
                  listPlexLibrariesMutation.mutate({ resourceId: value, connectionUri: selected?.base_url || '' })
                }}
              />
              <Select
                label="Connection"
                placeholder="Select which Plex endpoint to use"
                data={(plexResources.find((r) => r.resource_id === plexForm.resource_id)?.connections || []).map((conn) => ({
                  value: conn.uri,
                  label: `${conn.uri}${conn.local ? ' (Local)' : ''}${conn.relay ? ' (Relay)' : ''}`,
                }))}
                value={plexForm.connection_uri || null}
                onChange={(value) => {
                  if (!value || !plexForm.resource_id) return
                  updatePlexForm((prev) => ({ ...prev, connection_uri: value, base_url: value }))
                  setPlexLibraries([])
                  listPlexLibrariesMutation.mutate({ resourceId: plexForm.resource_id, connectionUri: value })
                }}
              />
            </SimpleGrid>
            <MultiSelect
              label="Libraries to refresh after a recording completes"
              placeholder="Choose one or more libraries"
              data={plexLibraries.map((lib) => ({
                value: String(lib.id),
                label: `${lib.title}${lib.type ? ` (${lib.type})` : ''}`,
              }))}
              value={(plexForm.library_section_ids || []).map((v) => String(v))}
              onChange={(values) => updatePlexForm((prev) => ({ ...prev, library_section_ids: values }))}
              searchable
            />
          </SubGroup>

          <SubGroup label="Behavior">
            <Stack gap={0}>
              <SettingRow
                label="Allow Plex server users to sign in"
                description="Anyone with access to your server can log in to Mustarrd and request recordings"
              >
                <Switch
                  size={switchSize}
                  aria-label="Allow Plex server users to sign in"
                  checked={plexForm.auto_allow_all_server_users}
                  onChange={(e) =>
                    updatePlexForm((prev) => ({ ...prev, auto_allow_all_server_users: e.currentTarget.checked }))
                  }
                />
              </SettingRow>
              <SettingRow
                label="Auto-refresh libraries after downloads"
                description="Trigger a Plex library scan when a recording finishes processing"
              >
                <Switch
                  size={switchSize}
                  aria-label="Auto-refresh libraries after downloads"
                  checked={plexForm.enabled}
                  onChange={(e) => updatePlexForm((prev) => ({ ...prev, enabled: e.currentTarget.checked }))}
                />
              </SettingRow>
            </Stack>
          </SubGroup>

          <Group justify="flex-end">
            <Button
              onClick={() =>
                savePlexMutation.mutate({
                  resource_id: plexForm.resource_id,
                  resource_name: plexForm.resource_name || null,
                  connection_uri: plexForm.connection_uri || plexForm.base_url,
                  base_url: plexForm.base_url,
                  machine_identifier: plexForm.machine_identifier || null,
                  library_section_ids: (plexForm.library_section_ids || []).map((s) => String(s)),
                  auto_allow_all_server_users: plexForm.auto_allow_all_server_users,
                  enabled: plexForm.enabled,
                })
              }
              disabled={!plexForm.resource_id || !plexForm.base_url}
              loading={savePlexMutation.isPending}
            >
              Save Plex Integration
            </Button>
          </Group>
        </>
      )}
    </Stack>
  )

  const comskipErrors = getComskipErrors(formData)

  const renderComskip = () => (
    <ComskipSection
      formData={formData}
      onChange={handleChange}
      onNavigateToProcessing={() => selectSection('processing')}
      hwDecodeModes={toolsStatus?.comskip_hw_decode_modes}
      onResetDefaults={() => {
        applyChanges({ ...COMSKIP_DEFAULTS })
      }}
    />
  )

  const sectionContent = {
    recording: renderRecording,
    processing: renderProcessing,
    comskip: renderComskip,
    naming: renderNaming,
    guide: renderGuide,
    appearance: renderAppearance,
    security: renderSecurity,
    users: renderUsers,
    plex: renderPlex,
    accounts: () => <AccountsSection showTitle={false} />,
    logs: () => <LogsSection showTitle={false} />,
  }
  const visibleSectionId = availableSections.some((section) => section.id === activeSection)
    ? activeSection
    : (availableSections[0]?.id || 'security')
  const activeSectionContent = sectionContent[visibleSectionId]

  const railGroups = isAdmin
    ? SECTION_GROUPS
    : [{ label: null, items: DOWNLOAD_USER_SECTIONS }]

  const renderRail = () => (
    <Stack gap={8} style={{ width: 218, flexShrink: 0, position: 'sticky', top: 16 }}>
      {isAdmin && <SettingsSearch onNavigate={selectSection} />}
      <Paper withBorder radius="md" p={8}>
        {railGroups.map((group) => (
          <div key={group.label || 'default'}>
            {group.label && <div className={classes.railGroupLabel}>{group.label}</div>}
            {group.items.map(({ id, label, icon: Icon }) => (
              <NavLink
                key={id}
                label={label}
                leftSection={<Icon size={15} />}
                rightSection={
                  id === 'accounts' && accountsNeedAttention ? (
                    <span className={classes.warnDot} title="A provider account needs attention" />
                  ) : null
                }
                active={visibleSectionId === id}
                onClick={() => selectSection(id)}
                styles={{
                  root: { borderRadius: 6, padding: '7px 10px' },
                  label: { fontSize: 13.5 },
                  section: { marginRight: 8 },
                }}
              />
            ))}
          </div>
        ))}
      </Paper>
    </Stack>
  )

  const renderMobileHome = () => (
    <Stack gap="xs">
      {isAdmin && <SettingsSearch isMobile onNavigate={selectSection} />}
      <div>
        {railGroups.map((group) => (
          <div key={group.label || 'default'}>
            {group.label && <div className={classes.mobileGroupLabel}>{group.label}</div>}
            {group.items.map(({ id, label, icon: Icon }) => (
              <button key={id} type="button" className={classes.mobileItem} onClick={() => selectSection(id)}>
                <span className={classes.mobileItemIcon}><Icon size={18} /></span>
                <span>{label}</span>
                {id === 'accounts' && accountsNeedAttention && (
                  <span className={classes.mobileWarnDot} title="A provider account needs attention" />
                )}
                <span className={classes.mobileItemChev}><IconChevronRight size={16} /></span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </Stack>
  )

  return (
    <Stack className={classes.page}>
      <Modal
        opened={leaveModalOpen}
        onClose={handleStay}
        title="Unsaved changes"
        centered
        size="md"
        radius="md"
        padding="lg"
        overlayProps={{ opacity: 0.55, blur: 2 }}
      >
        <Stack gap="md">
          <Alert icon={<IconAlertCircle size={16} />} color="yellow" variant="light" radius="md">
            <Text size="sm">
              You have unsaved settings changes. Save before leaving this page?
            </Text>
          </Alert>
          <Stack gap="xs">
            <Button
              onClick={handleSaveAndContinue}
              loading={isSavingAndLeaving || updateMutation.isPending}
              fullWidth
            >
              Save & Continue
            </Button>
            <Button variant="default" onClick={handleDiscardAndContinue} fullWidth>
              Discard & Continue
            </Button>
            <Button variant="subtle" color="gray" onClick={handleStay} fullWidth>
              Keep Editing
            </Button>
          </Stack>
        </Stack>
      </Modal>
      <Modal
        opened={resetPasswordModalOpen}
        onClose={() => {
          if (resetUserPasswordMutation.isPending) return
          setResetPasswordModalOpen(false)
          setResetTargetUser(null)
          setResetUserPassword('')
        }}
        title={`Set Password${resetTargetUser?.username ? ` for ${resetTargetUser.username}` : ''}`}
        centered
        size="sm"
      >
        <Stack gap="sm">
          <Text size="sm" c="dimmed">
            This temporary password is for next login. The user will be prompted to choose a new one immediately.
          </Text>
          <PasswordInput
            label="Temporary Password"
            description="Minimum 8 characters"
            value={resetUserPassword}
            onChange={(e) => setResetUserPassword(e.target.value)}
            autoComplete="new-password"
          />
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={() => {
                setResetPasswordModalOpen(false)
                setResetTargetUser(null)
                setResetUserPassword('')
              }}
              disabled={resetUserPasswordMutation.isPending}
            >
              Cancel
            </Button>
            <Button
              loading={resetUserPasswordMutation.isPending}
              onClick={() => {
                if (!resetTargetUser?.id) return
                if (!resetUserPassword || resetUserPassword.length < 8) {
                  notifications.show({
                    title: 'Password too short',
                    message: 'Temporary password must be at least 8 characters.',
                    color: 'yellow',
                  })
                  return
                }
                resetUserPasswordMutation.mutate({
                  userId: resetTargetUser.id,
                  password: resetUserPassword,
                })
              }}
            >
              Save Password
            </Button>
          </Group>
        </Stack>
      </Modal>

      {(!isMobile || mobileView === 'home') && (
        <Group justify="space-between">
          <Title order={2}>Settings</Title>
        </Group>
      )}

      {isMobile ? (
        mobileView === 'home' ? (
          renderMobileHome()
        ) : (
          <Stack gap="xs">
            <button type="button" className={classes.backBtn} onClick={goMobileHome}>
              <IconChevronLeft size={16} /> Settings
            </button>
            <Card shadow="sm" padding="md" radius="md" withBorder>
              <Box key={visibleSectionId} className={classes.fadeIn}>
                {activeSectionContent?.()}
              </Box>
            </Card>
          </Stack>
        )
      ) : (
        <Group align="flex-start" gap="lg" wrap="nowrap">
          {renderRail()}
          <Card shadow="sm" padding="lg" radius="md" withBorder style={{ flex: 1, minWidth: 0 }}>
            <Box key={visibleSectionId} className={classes.fadeIn}>
              {activeSectionContent?.()}
            </Box>
          </Card>
        </Group>
      )}

      {isAdmin && (
        <SaveBar
          count={changedCount}
          saving={updateMutation.isPending}
          disabled={Object.keys(comskipErrors).length > 0}
          onSave={handleSave}
          onDiscard={handleReset}
        />
      )}
    </Stack>
  )
}
