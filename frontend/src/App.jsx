import { useState, useEffect, useRef } from 'react'
import { Routes, Route, NavLink, Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  AppShell,
  Burger,
  Group,
  NavLink as MantineNavLink,
  Stack,
  Text,
  Box,
  Divider,
} from '@mantine/core'
import { useDisclosure, useMediaQuery } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import {
  IconSearch,
  IconDownload,
  IconCalendar,
  IconSettings,
  IconLogout,
  IconLogin,
} from '@tabler/icons-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import Browse from './pages/Browse'
import Downloads from './pages/Downloads'
import Scheduled from './pages/Scheduled'
import Settings from './pages/Settings'
import Login from './pages/Login'
import DownloadPlayer from './pages/DownloadPlayer'
import Onboarding from './pages/Onboarding'
import { authApi, downloadsApi, epgApi, onboardingApi, createDownloadWebSocket } from './api'

function ProtectedRoute({ authStatus, authLoading }) {
  const location = useLocation()

  if (authLoading) {
    return null
  }

  if (authStatus?.authenticated) {
    if (
      authStatus?.password_reset_required &&
      !authStatus?.is_admin &&
      location.pathname !== '/settings'
    ) {
      return <Navigate to="/settings?section=security" replace />
    }
    return <Outlet />
  }

  return <Navigate to="/login" replace state={{ from: location.pathname }} />
}

function AdminRoute({ authStatus, authLoading }) {
  if (authLoading) return null
  if (authStatus?.authenticated && authStatus?.is_admin) return <Outlet />
  return <Navigate to="/browse" replace />
}

function SettingsRoute({ authStatus, authLoading }) {
  if (authLoading) return null
  if (!authStatus?.authenticated) return <Navigate to="/login" replace />
  if (authStatus?.provider === 'plex' && !authStatus?.is_admin) return <Navigate to="/browse" replace />
  return <Outlet />
}

function OnboardingGate({ onboardingStatus, onboardingLoading }) {
  const location = useLocation()

  if (onboardingLoading) {
    return null
  }

  if (onboardingStatus?.completed === false && location.pathname !== '/onboarding') {
    return <Navigate to="/onboarding" replace state={{ from: location.pathname }} />
  }

  return <Outlet />
}

function App() {
  const [opened, { toggle, close }] = useDisclosure()
  const isMobile = useMediaQuery('(max-width: 48em)')
  const [activeDownloads, setActiveDownloads] = useState(0)
  const epgToastVisibleRef = useRef(false)
  const epgToastDismissedRef = useRef(false)
  const epgWasRunningRef = useRef(false)
  const epgToastCloseTimerRef = useRef(null)
  const epgToastPendingCloseRef = useRef(false)
  const queryClient = useQueryClient()
  const navigate = useNavigate()

  const { data: authStatus, isLoading: authLoading } = useQuery({
    queryKey: ['auth', 'status'],
    queryFn: authApi.status,
  })

  // Fetch download queue for badge
  const { data: queue } = useQuery({
    queryKey: ['downloads', 'queue'],
    queryFn: downloadsApi.getQueue,
    refetchInterval: 5000,
    enabled: Boolean(authStatus?.authenticated),
  })

  const { data: epgStatus } = useQuery({
    queryKey: ['epg', 'status'],
    queryFn: epgApi.status,
    refetchInterval: 5000,
    enabled: Boolean(authStatus?.authenticated && authStatus?.is_admin),
  })
  const { data: onboardingStatus, isLoading: onboardingLoading } = useQuery({
    queryKey: ['onboarding', 'status'],
    queryFn: onboardingApi.status,
    enabled: Boolean(authStatus?.authenticated && authStatus?.is_admin),
    refetchOnWindowFocus: true,
  })

  const logoutMutation = useMutation({
    mutationFn: authApi.logout,
    onSuccess: async () => {
      close()
      queryClient.setQueryData(['auth', 'status'], { authenticated: false, is_admin: false })
      await queryClient.invalidateQueries({ queryKey: ['auth', 'status'] })
      notifications.show({
        title: 'Logged out',
        color: 'yellow',
      })
      navigate('/login', { replace: true })
    },
  })

  useEffect(() => {
    if (queue) {
      setActiveDownloads(queue.length)
    }
  }, [queue])

  const clearEpgToastTimer = () => {
    if (epgToastCloseTimerRef.current) {
      clearTimeout(epgToastCloseTimerRef.current)
      epgToastCloseTimerRef.current = null
    }
  }

  const scheduleEpgToastClose = () => {
    clearEpgToastTimer()
    if (document.visibilityState === 'hidden') {
      epgToastPendingCloseRef.current = true
      return
    }
    epgToastPendingCloseRef.current = false
    epgToastCloseTimerRef.current = setTimeout(() => {
      notifications.hide('epg-download-progress')
      epgToastVisibleRef.current = false
    }, 10000)
  }

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible' && epgToastPendingCloseRef.current) {
        scheduleEpgToastClose()
      }
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [])

  useEffect(() => {
    if (!epgStatus) return

    const {
      running,
      account_name: accountName,
      processed_programs: processedPrograms,
      inserted_programs: insertedPrograms,
      total_programs: totalPrograms,
      last_error: lastError,
    } = epgStatus

    const processed = typeof processedPrograms === 'number' ? processedPrograms : 0
    const inserted = typeof insertedPrograms === 'number' ? insertedPrograms : 0
    const total = typeof totalPrograms === 'number' ? totalPrograms : null
    const hasTotal = total && total > 0
    const percent = hasTotal ? Math.min(100, Math.round((processed / total) * 100)) : null
    const percentLabel = percent != null ? ` • ${percent}%` : ''
    const title = accountName
      ? `Syncing EPG (${accountName}${percentLabel})`
      : 'Syncing EPG'
    const epgNotificationPosition = isMobile ? 'bottom-center' : 'top-right'
    const message = hasTotal
      ? `${inserted.toLocaleString()} new, ${processed.toLocaleString()} scanned of ${total.toLocaleString()} (${percent}%).`
      : `${inserted.toLocaleString()} new, ${processed.toLocaleString()} scanned so far.`

    if (running) {
      epgWasRunningRef.current = true
      if (epgToastDismissedRef.current) {
        return
      }
      clearEpgToastTimer()
      epgToastPendingCloseRef.current = false
      if (epgToastVisibleRef.current) {
        notifications.update({
          id: 'epg-download-progress',
          title,
          message,
          position: epgNotificationPosition,
          loading: true,
          autoClose: false,
          withCloseButton: true,
          onClose: () => {
            epgToastVisibleRef.current = false
            epgToastDismissedRef.current = true
          },
        })
      } else {
        notifications.show({
          id: 'epg-download-progress',
          title,
          message,
          position: epgNotificationPosition,
          loading: true,
          autoClose: false,
          withCloseButton: true,
          onClose: () => {
            epgToastVisibleRef.current = false
            epgToastDismissedRef.current = true
          },
        })
        epgToastVisibleRef.current = true
      }
      return
    }

    if (epgWasRunningRef.current) {
      epgWasRunningRef.current = false
      clearEpgToastTimer()
      epgToastPendingCloseRef.current = false
      notifications.hide('epg-download-progress')
      epgToastVisibleRef.current = false
      epgToastDismissedRef.current = false
      const resultToastId = `epg-download-result-${Date.now()}`
      if (lastError) {
        notifications.show({
          id: resultToastId,
          title: 'EPG download failed',
          message: lastError,
          position: epgNotificationPosition,
          color: 'red',
          loading: false,
          autoClose: 8000,
          withCloseButton: true,
        })
      } else {
        notifications.show({
          id: resultToastId,
          title: 'EPG download complete',
          message: `${inserted.toLocaleString()} new programs added.`,
          position: epgNotificationPosition,
          loading: false,
          autoClose: 4000,
          withCloseButton: true,
        })
      }
    }
  }, [epgStatus, isMobile])

  // WebSocket for real-time updates
  useEffect(() => {
    if (!authStatus?.authenticated) return undefined
    const ws = createDownloadWebSocket((data) => {
      if (data.type === 'progress') {
        if (data.status === 'completed' || data.status === 'failed' || data.status === 'cancelled') {
          setActiveDownloads((prev) => Math.max(0, prev - 1))
        }
      }
    })

    return () => ws.close()
  }, [authStatus?.authenticated])

  const navItems = [
    { icon: IconSearch, label: 'Browse', to: '/browse', authOnly: true },
    {
      icon: IconDownload,
      label: 'Downloads',
      to: '/downloads',
      badge: activeDownloads > 0 ? activeDownloads : null,
      authOnly: true,
    },
    { icon: IconCalendar, label: 'Scheduled', to: '/scheduled', authOnly: true },
    {
      icon: IconSettings,
      label: 'Settings',
      to: '/settings',
      authOnly: true,
      hideForPlexUsers: true,
    },
  ].filter((item) => {
    if (item.hideForPlexUsers && authStatus?.provider === 'plex' && !authStatus?.is_admin) return false
    if (item.adminOnly) return Boolean(authStatus?.authenticated && authStatus?.is_admin)
    if (item.authOnly) return Boolean(authStatus?.authenticated)
    return true
  })

  return (
    <AppShell
      header={{ height: { base: 56, sm: 0 } }}
      navbar={{ width: 250, breakpoint: 'sm', collapsed: { mobile: !opened } }}
      padding="md"
    >
      <AppShell.Header hiddenFrom="sm" style={{ borderBottom: '2px solid #f59f00' }}>
        <Group h="100%" px="md" justify="space-between">
          <Burger opened={opened} onClick={toggle} size="sm" />
          <Text
            style={{
              fontFamily: "'Bebas Neue', cursive",
              fontSize: 22,
              letterSpacing: '0.12em',
              color: '#f59f00',
              lineHeight: 1,
            }}
          >
            MUSTARRD
          </Text>
          <Group gap={5} align="center" style={{ visibility: activeDownloads > 0 ? 'visible' : 'hidden' }}>
            <Box
              style={{
                width: 7,
                height: 7,
                borderRadius: '50%',
                background: '#fa5252',
                animation: 'mustarrd-rec-pulse 1.5s ease-in-out infinite',
              }}
            />
            <Text size="xs" fw={700} c="red" style={{ letterSpacing: '0.12em' }}>
              REC
            </Text>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="md" style={{ borderTop: '3px solid #f59f00' }}>
        <Stack gap="md">
          <Group mb={4} justify="space-between" align="center" wrap="nowrap">
            <Text
              style={{
                fontFamily: "'Bebas Neue', cursive",
                fontSize: 28,
                letterSpacing: '0.12em',
                color: '#f59f00',
                lineHeight: 1,
              }}
            >
              MUSTARRD
            </Text>
            {activeDownloads > 0 && (
              <Group gap={5} align="center">
                <Box
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    background: '#fa5252',
                    animation: 'mustarrd-rec-pulse 1.5s ease-in-out infinite',
                  }}
                />
                <Text size="xs" fw={700} c="red" style={{ letterSpacing: '0.12em' }}>
                  REC
                </Text>
              </Group>
            )}
          </Group>
          <Divider mb={4} />
          {navItems.map((item) => (
            <MantineNavLink
              key={item.to}
              component={NavLink}
              to={item.to}
              onClick={close}
              label={item.label}
              leftSection={<item.icon size={20} />}
              rightSection={
                item.badge ? (
                  <Box
                    style={{
                      minWidth: 20,
                      height: 20,
                      borderRadius: '50%',
                      background: '#f59f00',
                      color: '#1a1b1e',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 11,
                      fontWeight: 700,
                      lineHeight: 1,
                      padding: '0 5px',
                    }}
                  >
                    {item.badge}
                  </Box>
                ) : null
              }
              style={{ borderRadius: 8, marginBottom: 4 }}
            />
          ))}
          <Divider />
          {authStatus?.authenticated ? (
            <MantineNavLink
              component="button"
              label="Log Out"
              leftSection={<IconLogout size={20} />}
              onClick={() => logoutMutation.mutate()}
              disabled={logoutMutation.isPending}
              style={{ borderRadius: 8 }}
              styles={{ label: { color: 'var(--mantine-color-dimmed)' } }}
            />
          ) : (
            <MantineNavLink
              component={NavLink}
              to="/login"
              onClick={close}
              label="Sign In"
              leftSection={<IconLogin size={20} />}
              style={{ borderRadius: 8 }}
            />
          )}
        </Stack>
      </AppShell.Navbar>

      <AppShell.Main>
        <Routes>
          <Route path="/" element={<Navigate to="/browse" replace />} />
          <Route path="/login" element={<Login />} />
          <Route element={<ProtectedRoute authStatus={authStatus} authLoading={authLoading} />}>
            <Route element={<AdminRoute authStatus={authStatus} authLoading={authLoading} />}>
              <Route path="/onboarding" element={<Onboarding />} />
              <Route element={<OnboardingGate onboardingStatus={onboardingStatus} onboardingLoading={onboardingLoading} />}>
                <Route path="/accounts" element={<Navigate to="/settings?section=accounts" replace />} />
                <Route path="/logs" element={<Navigate to="/settings?section=logs" replace />} />
              </Route>
            </Route>
            <Route element={<SettingsRoute authStatus={authStatus} authLoading={authLoading} />}>
              <Route path="/settings" element={<Settings />} />
            </Route>
            <Route path="/browse" element={<Browse />} />
            <Route path="/downloads" element={<Downloads />} />
            <Route path="/downloads/:downloadId/play" element={<DownloadPlayer />} />
            <Route path="/scheduled" element={<Scheduled />} />
          </Route>
        </Routes>
      </AppShell.Main>
    </AppShell>
  )
}

export default App
