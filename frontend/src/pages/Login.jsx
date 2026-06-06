import { useEffect, useRef, useState } from 'react'
import { Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Card,
  Stack,
  PasswordInput,
  Button,
  Text,
  Alert,
  Loader,
  Box,
  Group,
  TextInput,
  Divider,
} from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { IconAlertCircle, IconServer } from '@tabler/icons-react'

import { authApi, onboardingApi } from '../api'

export default function Login() {
  const [username, setUsername] = useState('')
  const [setupUsername, setSetupUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [bootstrapUsername, setBootstrapUsername] = useState('admin')
  const [bootstrapPassword, setBootstrapPassword] = useState('')
  const [error, setError] = useState('')
  const [plexAuthUrl, setPlexAuthUrl] = useState('')
  const pollTimerRef = useRef(null)
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const fromPath = location.state?.from || '/browse'
  const setupToken = searchParams.get('setup_token')

  const { data: status, isLoading } = useQuery({
    queryKey: ['auth', 'status'],
    queryFn: authApi.status,
    refetchOnWindowFocus: true,
  })

  const { data: setupInfo, isLoading: setupLoading } = useQuery({
    queryKey: ['auth', 'setup-token', setupToken],
    queryFn: () => authApi.validateSetupToken(setupToken),
    enabled: Boolean(setupToken),
    retry: false,
  })

  useEffect(() => {
    if (status?.bootstrap_required) {
      setBootstrapUsername('admin')
    }
  }, [status?.bootstrap_required])
  useEffect(() => {
    if (!status?.password_set) {
      setSetupUsername('admin')
    }
  }, [status?.password_set])

  useEffect(() => {
    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current)
        pollTimerRef.current = null
      }
    }
  }, [])

  const handleLoginSuccess = (target = fromPath) => {
    navigate(target, { replace: true })
    queryClient.invalidateQueries({ queryKey: ['auth', 'status'] })
  }

  const resolvePostLoginTarget = async (defaultTarget = fromPath) => {
    try {
      const authStatus = await authApi.status()
      if (authStatus?.authenticated && authStatus?.is_admin) {
        const onboardingStatus = await onboardingApi.status()
        if (onboardingStatus?.completed === false) {
          return '/onboarding'
        }
      }
    } catch {
      // Fall back to default target if status checks fail.
    }
    return defaultTarget
  }

  const setupMutation = useMutation({
    mutationFn: ({ pass, user }) => authApi.setup(pass, user),
    onSuccess: () => {
      handleLoginSuccess('/onboarding')
    },
    onError: (err) => setError(err.message),
  })

  const credentialsMutation = useMutation({
    mutationFn: ({ user, pass }) => authApi.loginCredentials(user, pass),
    onSuccess: async () => {
      const target = await resolvePostLoginTarget(fromPath)
      handleLoginSuccess(target)
    },
    onError: (err) => setError(err.message),
  })

  const bootstrapMutation = useMutation({
    mutationFn: ({ user, pass }) => authApi.adminBootstrapComplete(user, pass),
    onSuccess: () => {
      handleLoginSuccess('/onboarding')
    },
    onError: (err) => setError(err.message),
  })

  const setupTokenMutation = useMutation({
    mutationFn: (value) => authApi.completeSetupToken(setupToken, value),
    onSuccess: () => {
      setError('')
      setPassword('')
    },
    onError: (err) => setError(err.message),
  })

  const plexStartMutation = useMutation({
    mutationFn: () => authApi.plexStart(),
    onSuccess: (data, variables) => {
      const pinId = data.id
      const pollEveryMs = Math.max(1, Number(data.poll_interval_seconds || 2)) * 1000
      const timeoutMs = Math.max(30, Number(data.expires_in || 300)) * 1000

      const popup = variables?.popupRef || null
      setPlexAuthUrl(data.auth_url)
      if (popup && !popup.closed) {
        try {
          popup.opener = null
          popup.location.replace(data.auth_url)
        } catch {
          // noop; fallback link remains visible
        }
      }
      const startedAt = Date.now()
      if (pollTimerRef.current) clearInterval(pollTimerRef.current)

      pollTimerRef.current = setInterval(async () => {
        if (Date.now() - startedAt > timeoutMs) {
          clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
          setError('Plex login timed out. Please try again.')
          return
        }
        try {
          await authApi.plexComplete(pinId)
          clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
          try {
            if (popup && !popup.closed) popup.close()
          } catch {
            // noop
          }
          setPlexAuthUrl('')
          const target = await resolvePostLoginTarget(fromPath)
          handleLoginSuccess(target)
        } catch (err) {
          if (err?.status === 202 || (err?.message || '').includes('not complete')) {
            return
          }
          clearInterval(pollTimerRef.current)
          pollTimerRef.current = null
          setError(err.message || 'Plex login failed')
        }
      }, pollEveryMs)
    },
    onError: (err) => setError(err.message),
  })

  if (isLoading || setupLoading) {
    return (
      <Stack align="center" justify="center" h={320}>
        <Loader />
      </Stack>
    )
  }

  if (status?.authenticated) {
    return <Navigate to={fromPath} replace />
  }

  const isSetupMode = !status?.password_set
  const isBootstrapMode = Boolean(status?.bootstrap_required)
  const isSubmitting =
    setupMutation.isPending ||
    credentialsMutation.isPending ||
    bootstrapMutation.isPending ||
    plexStartMutation.isPending ||
    setupTokenMutation.isPending

  const handleSubmit = (e) => {
    e.preventDefault()
    setError('')

    if (setupToken) {
      if (!password || password.length < 8) {
        setError('Password must be at least 8 characters')
        return
      }
      setupTokenMutation.mutate(password)
      return
    }

    if (isSetupMode) {
      if (!password || password.length < 8) {
        setError('Password must be at least 8 characters')
        return
      }
      if (!setupUsername.trim()) {
        setError('Admin username is required')
        return
      }
      setupMutation.mutate({ pass: password, user: setupUsername.trim() })
      return
    }

    if (isBootstrapMode) {
      if (!bootstrapUsername.trim()) {
        setError('Username is required')
        return
      }
      if (!bootstrapPassword) {
        setError('Legacy admin password is required')
        return
      }
      bootstrapMutation.mutate({ user: bootstrapUsername.trim(), pass: bootstrapPassword })
      return
    }

    if (!username.trim()) {
      setError('Username is required')
      return
    }
    if (!password) {
      setError('Password is required')
      return
    }
    credentialsMutation.mutate({ user: username.trim(), pass: password })
  }

  return (
    <Box
      style={{
        minHeight: 'calc(100vh - 4rem)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '2rem',
      }}
    >
      <Card withBorder shadow="md" w="100%" maw={440} radius="lg" p={0} style={{ borderTop: '3px solid #f59f00', overflow: 'hidden' }}>
        <Stack px="lg" pt="lg" pb="xl" gap="md">
          <Stack gap={4}>
            <Text fw={600} size="xl">
              {setupToken
                ? 'Set Your Password'
                : isSetupMode
                ? 'Welcome to Mustarrd'
                : isBootstrapMode
                ? 'Choose Admin Username'
                : 'Sign In'}
            </Text>
            {(setupToken || isSetupMode || isBootstrapMode) && (
              <Text size="sm" c="dimmed">
                {setupToken
                  ? `Create a password for ${setupInfo?.display_name || setupInfo?.username || 'your account'}.`
                  : isSetupMode
                  ? 'Please set an admin username and password.'
                  : 'Existing install detected. Choose an admin username and confirm your current admin password.'}
              </Text>
            )}
          </Stack>

          {error && <Alert color="red" icon={<IconAlertCircle size={16} />} radius="md">{error}</Alert>}
          {!setupToken && !isSetupMode && !isBootstrapMode && plexAuthUrl && (
            <Alert color="blue" icon={<IconServer size={16} />} radius="md">
              <Group justify="space-between" align="center">
                <Text size="sm">If Plex did not open automatically, tap to continue.</Text>
                <Button
                  component="a"
                  href={plexAuthUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  variant="light"
                  size="xs"
                >
                  Open Plex Login
                </Button>
              </Group>
            </Alert>
          )}

          <form onSubmit={handleSubmit}>
            <Stack gap="sm">
              {!setupToken && !isSetupMode && !isBootstrapMode && (
                <TextInput label="Username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
              )}

              {!setupToken && isSetupMode && (
                <TextInput label="Admin Username" value={setupUsername} onChange={(e) => setSetupUsername(e.target.value)} autoFocus required />
              )}

              {!setupToken && isBootstrapMode && (
                <TextInput label="Admin Username" value={bootstrapUsername} onChange={(e) => setBootstrapUsername(e.target.value)} autoFocus required />
              )}

              <PasswordInput
                label={isBootstrapMode ? 'Current Admin Password' : 'Password'}
                value={isBootstrapMode ? bootstrapPassword : password}
                onChange={(e) => (isBootstrapMode ? setBootstrapPassword(e.target.value) : setPassword(e.target.value))}
                autoFocus={isSetupMode || Boolean(setupToken)}
                required
                size="md"
                description={(isSetupMode || Boolean(setupToken)) ? 'Minimum 8 characters' : undefined}
              />

              <Button type="submit" loading={isSubmitting} size="md" color="yellow" fullWidth mt={4}>
                {setupToken ? 'Save Password' : isSetupMode ? 'Save Password' : isBootstrapMode ? 'Complete Admin Setup' : 'Sign In'}
              </Button>
            </Stack>
          </form>

          {!setupToken && !isSetupMode && !isBootstrapMode && status?.plex_login_available && (
            <>
              <Divider label="or" labelPosition="center" />
              <Button
                variant="light"
                leftSection={<IconServer size={16} />}
                onClick={() => {
                  setError('')
                  setPlexAuthUrl('')
                  const popup = window.open('about:blank', '_blank')
                  plexStartMutation.mutate({ popupRef: popup })
                }}
                loading={plexStartMutation.isPending}
              >
                Sign In with Plex
              </Button>
            </>
          )}
        </Stack>
      </Card>
    </Box>
  )
}
