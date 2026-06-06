import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  Checkbox,
  Group,
  Loader,
  MultiSelect,
  PasswordInput,
  Radio,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { IconAlertCircle, IconCheck } from '@tabler/icons-react'

import { accountsApi, adminPlexApi, onboardingApi } from '../api'
import classes from './Onboarding.module.css'

function isPlexNotConnectedError(message) {
  return (message || '').toLowerCase().includes('plex account is not connected')
}

function getMaxUnlockedStep(status) {
  if (!status) return 0
  if ((status.account_count || 0) < 1) return 0
  if (!status.processing_confirmed || !status.comskip_confirmed) return 1
  return 2
}

export default function Onboarding() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const isMobile = useMediaQuery('(max-width: 48em)')
  const [currentStep, setCurrentStep] = useState(0)
  const [selectedProfile, setSelectedProfile] = useState('')
  const [comskipChoice, setComskipChoice] = useState('enable')
  const [acknowledgeUnavailable, setAcknowledgeUnavailable] = useState(false)
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
  const [accountForm, setAccountForm] = useState({
    name: '',
    server_url: '',
    username: '',
    password: '',
  })

  const { data: status, isLoading, error } = useQuery({
    queryKey: ['onboarding', 'status'],
    queryFn: onboardingApi.status,
    refetchOnWindowFocus: true,
  })
  const { data: plexConfig } = useQuery({
    queryKey: ['admin', 'plex'],
    queryFn: adminPlexApi.get,
    refetchOnWindowFocus: false,
  })

  const createAccountMutation = useMutation({
    mutationFn: accountsApi.create,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['accounts'] })
      await queryClient.invalidateQueries({ queryKey: ['accounts', 'public'] })
      await queryClient.invalidateQueries({ queryKey: ['onboarding', 'status'] })
      setAccountForm((prev) => ({ ...prev, password: '' }))
      setCurrentStep(1)
      notifications.show({
        title: 'Account Added',
        message: 'Xtream account connected successfully.',
        color: 'green',
      })
    },
    onError: (err) => {
      notifications.show({
        title: 'Could not add account',
        message: err.message,
        color: 'red',
      })
    },
  })

  const saveProcessingMutation = useMutation({
    mutationFn: async ({ profile, comskipEnabled, acknowledgeUnavailable }) => {
      await onboardingApi.applyProcessingProfile(profile)
      await onboardingApi.setComskipPolicy(comskipEnabled, acknowledgeUnavailable)
      return onboardingApi.status()
    },
    onSuccess: async (nextStatus) => {
      await queryClient.invalidateQueries({ queryKey: ['settings'] })
      await queryClient.invalidateQueries({ queryKey: ['onboarding', 'status'] })
      if (nextStatus?.completed) {
        setCurrentStep(2)
      } else {
        notifications.show({
          title: 'Setup incomplete',
          message: 'Account, processing, and Comskip must be completed to continue.',
          color: 'yellow',
        })
      }
    },
    onError: (err) => {
      notifications.show({
        title: 'Could not save setup',
        message: err.message,
        color: 'red',
      })
    },
  })
  const savePlexMutation = useMutation({
    mutationFn: adminPlexApi.save,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['admin', 'plex'] })
      notifications.show({
        title: 'Plex Saved',
        message: 'Plex user and library sync settings updated.',
        color: 'green',
      })
      navigate('/browse', { replace: true })
    },
    onError: (err) => {
      notifications.show({
        title: 'Plex Save Failed',
        message: err.message,
        color: 'red',
      })
    },
  })
  const listPlexResourcesMutation = useMutation({
    mutationFn: adminPlexApi.listResources,
    onSuccess: (resources) => {
      setPlexResources(resources || [])
    },
    onError: (err) => {
      if (isPlexNotConnectedError(err.message)) {
        notifications.show({
          title: 'Connect Plex First',
          message: 'Connect a Plex account to load available servers.',
          color: 'yellow',
        })
        return
      }
      notifications.show({
        title: 'Plex Resources Failed',
        message: err.message,
        color: 'red',
      })
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
        setPlexForm((prev) => ({
          ...prev,
          connection_uri: data.connection_uri || data.base_url,
          base_url: data.base_url,
        }))
      }
    },
    onError: (err) => {
      notifications.show({
        title: 'Plex Libraries Failed',
        message: err.message,
        color: 'red',
      })
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
          notifications.show({
            title: 'Plex Connection Timeout',
            message: 'Please try connecting again.',
            color: 'yellow',
          })
          return
        }

        try {
          const connectStatus = await adminPlexApi.connectComplete(pinId)
          if (connectStatus?.status !== 'connected') return

          clearInterval(timer)
          try {
            if (popup && !popup.closed) popup.close()
          } catch {
            // noop
          }
          notifications.show({
            title: 'Plex Connected',
            message: 'Choose a server and libraries to enable sync.',
            color: 'green',
          })
          listPlexResourcesMutation.mutate()
        } catch (err) {
          if ((err.message || '').includes('not complete')) return
          clearInterval(timer)
          notifications.show({
            title: 'Plex Connection Failed',
            message: err.message,
            color: 'red',
          })
        }
      }, pollMs)
    },
    onError: (err) => {
      notifications.show({
        title: 'Plex Connection Failed',
        message: err.message,
        color: 'red',
      })
    },
  })

  const availableProfiles = useMemo(() => (status?.profiles || []).filter((profile) => profile.available), [status])

  useEffect(() => {
    if (!status) return

    if (!selectedProfile) {
      setSelectedProfile(status.selected_profile || status.recommended_profile || '')
    }

    const comskipAvailable = Boolean(status?.tools?.comskip?.available)
    if (status.comskip_confirmed) {
      setComskipChoice(status.comskip_enabled ? 'enable' : 'disable_with_ack')
    } else if (comskipAvailable) {
      setComskipChoice('enable')
    } else {
      setComskipChoice('disable_with_ack')
    }

    const maxUnlocked = getMaxUnlockedStep(status)
    setCurrentStep((prev) => (prev > maxUnlocked ? maxUnlocked : prev))
  }, [navigate, selectedProfile, status])

  useEffect(() => {
    if (!plexConfig) return
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
  }, [plexConfig])

  useEffect(() => {
    if (currentStep !== 2) return
    if (!plexConfig?.token_configured) return
    if (!plexForm.resource_id) return
    if (plexLibraries.length > 0) return
    listPlexLibrariesMutation.mutate({
      resourceId: plexForm.resource_id,
      connectionUri: plexForm.connection_uri || plexForm.base_url,
    })
  }, [currentStep, plexConfig?.token_configured, plexForm.base_url, plexForm.connection_uri, plexForm.resource_id, plexLibraries.length])

  if (isLoading) {
    return (
      <Stack align="center" justify="center" h={320}>
        <Loader />
      </Stack>
    )
  }

  if (error) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={16} />}>
        {error.message}
      </Alert>
    )
  }

  const accountComplete = (status?.account_count || 0) > 0
  const comskipAvailable = Boolean(status?.tools?.comskip?.available)
  const isBusy =
    createAccountMutation.isPending ||
    saveProcessingMutation.isPending ||
    savePlexMutation.isPending ||
    connectPlexStartMutation.isPending ||
    listPlexResourcesMutation.isPending ||
    listPlexLibrariesMutation.isPending

  const canOpenStep = (step) => step <= getMaxUnlockedStep(status)

  const handleCreateAccount = (e) => {
    e.preventDefault()
    createAccountMutation.mutate(accountForm)
  }

  const handleSaveProcessingAndComskip = () => {
    if (!selectedProfile) {
      notifications.show({
        title: 'Select a profile',
        message: 'Choose a processing profile to continue.',
        color: 'yellow',
      })
      return
    }
    if (comskipAvailable) {
      saveProcessingMutation.mutate({
        profile: selectedProfile,
        comskipEnabled: comskipChoice === 'enable',
        acknowledgeUnavailable: false,
      })
      return
    }

    if (!acknowledgeUnavailable) {
      notifications.show({
        title: 'Confirm fallback',
        message: 'Acknowledge that Comskip is unavailable to continue.',
        color: 'yellow',
      })
      return
    }

    saveProcessingMutation.mutate({
      profile: selectedProfile,
      comskipEnabled: false,
      acknowledgeUnavailable: true,
    })
  }

  const handleFinish = () => {
    navigate('/browse', { replace: true })
  }

  return (
    <Stack gap="md">
      <Group justify="space-between" align="center" wrap="nowrap">
        <div>
          <Title order={isMobile ? 3 : 2}>First-Time Setup</Title>
        </div>
        <Badge color="yellow" variant="light" size={isMobile ? 'md' : 'lg'}>Required</Badge>
      </Group>

      <Card withBorder p={isMobile ? 'sm' : 'md'}>
        <div className={classes.stepHeader}>
          <button
            type="button"
            className={`${classes.stepButton} ${currentStep === 0 ? classes.stepButtonActive : ''}`}
            onClick={() => {
              if (!isBusy && canOpenStep(0)) setCurrentStep(0)
            }}
          >
            <span className={classes.stepIcon}>{1}</span>
            <span className={classes.stepBody}>
              <span className={classes.stepLabel}>Account</span>
              <span className={classes.stepDescription}>Add provider</span>
            </span>
          </button>

          <div className={`${classes.stepConnector} ${currentStep > 0 ? classes.stepConnectorActive : ''}`} />

          <button
            type="button"
            className={`${classes.stepButton} ${currentStep === 1 ? classes.stepButtonActive : ''}`}
            onClick={() => {
              if (!isBusy && canOpenStep(1)) setCurrentStep(1)
            }}
            disabled={!canOpenStep(1) || isBusy}
          >
            <span className={classes.stepIcon}>{2}</span>
            <span className={classes.stepBody}>
              <span className={classes.stepLabel}>Processing + Comskip</span>
              <span className={classes.stepDescription}>Set defaults</span>
            </span>
          </button>

          <div className={`${classes.stepConnector} ${currentStep > 1 ? classes.stepConnectorActive : ''}`} />

          <button
            type="button"
            className={`${classes.stepButton} ${currentStep === 2 ? classes.stepButtonActive : ''}`}
            onClick={() => {
              if (!isBusy && canOpenStep(2)) setCurrentStep(2)
            }}
            disabled={!canOpenStep(2) || isBusy}
          >
            <span className={classes.stepIcon}>{3}</span>
            <span className={classes.stepBody}>
              <span className={classes.stepLabel}>Plex Sync</span>
              <span className={classes.stepDescription}>Optional</span>
            </span>
          </button>
        </div>

        {currentStep === 0 && (
          <Stack gap="md" mt="md">
            {accountComplete && (
              <Alert color="green" icon={<IconCheck size={16} />}>
                At least one account is connected ({status.account_count}).
              </Alert>
            )}

            <form onSubmit={handleCreateAccount}>
              <Stack>
                <TextInput
                  label="Account Name"
                  placeholder="My IPTV Provider"
                  size={isMobile ? 'md' : 'sm'}
                  required
                  value={accountForm.name}
                  onChange={(e) => setAccountForm((prev) => ({ ...prev, name: e.target.value }))}
                />
                <TextInput
                  label="Server URL"
                  placeholder="https://provider.example.com"
                  size={isMobile ? 'md' : 'sm'}
                  required
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  inputMode="url"
                  value={accountForm.server_url}
                  onChange={(e) => setAccountForm((prev) => ({ ...prev, server_url: e.target.value.toLowerCase() }))}
                />
                <TextInput
                  label="Username"
                  size={isMobile ? 'md' : 'sm'}
                  required
                  value={accountForm.username}
                  onChange={(e) => setAccountForm((prev) => ({ ...prev, username: e.target.value }))}
                />
                <PasswordInput
                  label="Password"
                  size={isMobile ? 'md' : 'sm'}
                  required
                  value={accountForm.password}
                  onChange={(e) => setAccountForm((prev) => ({ ...prev, password: e.target.value }))}
                />
                <Box
                  mt="sm"
                  style={{
                    position: isMobile ? 'sticky' : 'static',
                    bottom: isMobile ? 0 : 'auto',
                    background: 'var(--mantine-color-body)',
                    paddingTop: isMobile ? 8 : 0,
                    zIndex: 5,
                  }}
                >
                  <Group justify="space-between" grow={isMobile}>
                    <Button type="submit" loading={createAccountMutation.isPending} fullWidth={isMobile}>
                      {accountComplete ? 'Add Another Account' : 'Save & Continue'}
                    </Button>
                    {accountComplete && (
                      <Button
                        variant="light"
                        onClick={() => setCurrentStep(1)}
                        disabled={isBusy}
                        fullWidth={isMobile}
                      >
                        Continue
                      </Button>
                    )}
                  </Group>
                </Box>
              </Stack>
            </form>
          </Stack>
        )}

        {currentStep === 1 && (
          <Stack gap="md" mt="md">
            <Radio.Group value={selectedProfile} onChange={setSelectedProfile}>
              <Stack gap="xs">
                {availableProfiles.map((profile) => (
                  <Card key={profile.id} withBorder padding="sm">
                    <Radio value={profile.id} label={profile.label} description={profile.description} />
                    {profile.recommended && (
                      <Badge mt={8} size="xs" color="orange" variant="outline">
                        Recommended
                      </Badge>
                    )}
                  </Card>
                ))}
              </Stack>
            </Radio.Group>

            {comskipAvailable ? (
              <Radio.Group value={comskipChoice} onChange={setComskipChoice}>
                <Stack gap="xs">
                  <Radio value="enable" label="Enable Comskip" description="Detect and remove commercials." />
                  <Radio value="disable_with_ack" label="Disable Comskip" description="Keep commercials in recordings." />
                </Stack>
              </Radio.Group>
            ) : (
              <>
                <Alert color="yellow" icon={<IconAlertCircle size={16} />}>
                  Comskip is unavailable: {status?.tools?.comskip?.error || 'Tool not detected'}
                </Alert>
                <Checkbox
                  checked={acknowledgeUnavailable}
                  onChange={(e) => setAcknowledgeUnavailable(e.currentTarget.checked)}
                  label="I understand setup will continue with Comskip disabled."
                />
              </>
            )}

            <Group justify="space-between">
              <Box
                style={{
                  width: '100%',
                  position: isMobile ? 'sticky' : 'static',
                  bottom: isMobile ? 0 : 'auto',
                  background: 'var(--mantine-color-body)',
                  paddingTop: isMobile ? 8 : 0,
                  zIndex: 5,
                }}
              >
                <Group justify="space-between" grow={isMobile}>
                  <Button variant="subtle" onClick={() => setCurrentStep(0)} disabled={isBusy} fullWidth={isMobile}>
                    Back
                  </Button>
                  <Button onClick={handleSaveProcessingAndComskip} loading={saveProcessingMutation.isPending} fullWidth={isMobile}>
                    Save & Continue
                  </Button>
                </Group>
              </Box>
            </Group>
          </Stack>
        )}

        {currentStep === 2 && (
          <Stack gap="md" mt="md">
            <Alert color="blue" icon={<IconAlertCircle size={16} />}>
              Plex user access and library refresh are optional. You can skip this and finish onboarding now.
            </Alert>

            <Group justify="space-between" align="center">
              <Button
                variant="light"
                onClick={() => {
                  const popup = window.open('about:blank', '_blank')
                  connectPlexStartMutation.mutate({ popupRef: popup })
                }}
                loading={connectPlexStartMutation.isPending}
              >
                Connect Plex Account
              </Button>
              <Button
                variant="subtle"
                onClick={() => listPlexResourcesMutation.mutate()}
                loading={listPlexResourcesMutation.isPending}
              >
                Refresh Servers
              </Button>
            </Group>

            <Select
              label="Plex Server"
              placeholder="Select an owned server"
              data={plexResources.map((resource) => ({
                value: resource.resource_id,
                label: `${resource.name}${resource.platform ? ` (${resource.platform})` : ''}`,
              }))}
              value={plexForm.resource_id || null}
              onChange={(value) => {
                if (!value) return
                const selected = plexResources.find((resource) => resource.resource_id === value)
                setPlexForm((prev) => ({
                  ...prev,
                  resource_id: value,
                  resource_name: selected?.name || '',
                  machine_identifier: selected?.machine_identifier || null,
                  connection_uri: selected?.base_url || '',
                  base_url: selected?.base_url || '',
                }))
                setPlexLibraries([])
                listPlexLibrariesMutation.mutate({
                  resourceId: value,
                  connectionUri: selected?.base_url || '',
                })
              }}
            />

            <Select
              label="Connection URI"
              placeholder="Select which Plex endpoint to use"
              data={(plexResources.find((resource) => resource.resource_id === plexForm.resource_id)?.connections || []).map((conn) => ({
                value: conn.uri,
                label: `${conn.uri}${conn.local ? ' (Local)' : ''}${conn.relay ? ' (Relay)' : ''}`,
              }))}
              value={plexForm.connection_uri || null}
              onChange={(value) => {
                if (!value || !plexForm.resource_id) return
                setPlexForm((prev) => ({ ...prev, connection_uri: value, base_url: value }))
                setPlexLibraries([])
                listPlexLibrariesMutation.mutate({
                  resourceId: plexForm.resource_id,
                  connectionUri: value,
                })
              }}
            />

            <MultiSelect
              label="Libraries to Refresh"
              placeholder="Choose one or more libraries"
              data={plexLibraries.map((lib) => ({
                value: String(lib.id),
                label: `${lib.title}${lib.type ? ` (${lib.type})` : ''}`,
              }))}
              value={(plexForm.library_section_ids || []).map((value) => String(value))}
              onChange={(values) => setPlexForm((prev) => ({ ...prev, library_section_ids: values }))}
              searchable
            />

            <Switch
              label="Allow Plex server users to sign in to Mustarrd"
              checked={plexForm.auto_allow_all_server_users}
              onChange={(e) =>
                setPlexForm((prev) => ({ ...prev, auto_allow_all_server_users: e.currentTarget.checked }))
              }
            />
            <Switch
              label="Auto-refresh selected Plex libraries after downloads"
              checked={plexForm.enabled}
              onChange={(e) => setPlexForm((prev) => ({ ...prev, enabled: e.currentTarget.checked }))}
            />

            <Box
              style={{
                width: '100%',
                position: isMobile ? 'sticky' : 'static',
                bottom: isMobile ? 0 : 'auto',
                background: 'var(--mantine-color-body)',
                paddingTop: isMobile ? 8 : 0,
                zIndex: 5,
              }}
            >
              <Group justify="space-between" grow={isMobile}>
                <Button variant="subtle" onClick={() => setCurrentStep(1)} disabled={isBusy} fullWidth={isMobile}>
                  Back
                </Button>
                <Button variant="default" onClick={handleFinish} disabled={isBusy} fullWidth={isMobile}>
                  Skip For Now
                </Button>
                <Button
                  onClick={() =>
                    savePlexMutation.mutate({
                      resource_id: plexForm.resource_id,
                      resource_name: plexForm.resource_name || null,
                      connection_uri: plexForm.connection_uri || plexForm.base_url,
                      base_url: plexForm.base_url,
                      machine_identifier: plexForm.machine_identifier || null,
                      library_section_ids: (plexForm.library_section_ids || []).map((value) => String(value)),
                      auto_allow_all_server_users: plexForm.auto_allow_all_server_users,
                      enabled: plexForm.enabled,
                    })
                  }
                  disabled={!plexForm.resource_id || !plexForm.base_url}
                  loading={savePlexMutation.isPending}
                  fullWidth={isMobile}
                >
                  Save Plex + Finish
                </Button>
              </Group>
            </Box>
          </Stack>
        )}
      </Card>
    </Stack>
  )
}
