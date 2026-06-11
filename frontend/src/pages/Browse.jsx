import { useState, useMemo, useEffect, useRef, useCallback } from 'react'
import {
  Title,
  Select,
  Card,
  Group,
  Text,
  Stack,
  TextInput,
  ScrollArea,
  ActionIcon,
  Badge,
  Loader,
  Tabs,
  Button,
  Switch,
} from '@mantine/core'
import { useDebouncedValue, useMediaQuery } from '@mantine/hooks'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  IconSearch,
  IconAlertCircle,
  IconVideo,
  IconPlayerPlay,
  IconChevronLeft,
  IconStar,
  IconStarFilled,
  IconDownload,
  IconCalendar,
} from '@tabler/icons-react'
import dayjs from 'dayjs'

import { accountsApi, authApi, channelsApi, downloadsApi, epgApi, settingsApi, vodApi } from '../api'
import EPGGrid from '../components/EPGGrid'
import DownloadModal from '../components/DownloadModal'
import PreviewModal from '../components/PreviewModal'
import ScheduleModal from '../components/ScheduleModal'
import MovieModal from '../components/VodMovieModal'
import SeriesModal from '../components/VodSeriesModal'
import {
  formatChannelDateTime,
  getChannelDisplayTime,
  getGuideOffsetHours,
  getProgramInstant,
  getNowUtc,
} from '../utils/channelTime'
import classes from './Browse.module.css'
import rowClasses from '../components/EPGGrid.module.css'

function channelInitials(name) {
  const words = (name || '').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '?'
  return words
    .slice(0, 2)
    .map((word) => word[0])
    .join('')
    .toUpperCase()
}

function channelLogoSrc(streamIcon) {
  if (!streamIcon || !/^https?:\/\//i.test(streamIcon)) return null
  // Logos go through the backend cache so they load from disk after the
  // first fetch instead of hitting the provider host on every render.
  return `/api/logos?url=${encodeURIComponent(streamIcon)}`
}

function ChannelLogo({ channel, className }) {
  const [failed, setFailed] = useState(false)
  const src = channelLogoSrc(channel?.stream_icon)
  // The guide header reuses one ChannelLogo across channel switches, so a
  // failure on one channel must not hide the next channel's logo.
  useEffect(() => setFailed(false), [src])
  const showImage = src && !failed
  const baseClass = className || classes.chLogo
  return (
    <span className={showImage ? `${baseClass} ${classes.chLogoImage}` : baseClass}>
      {showImage ? (
        <img src={src} alt="" loading="lazy" onError={() => setFailed(true)} />
      ) : (
        channelInitials(channel?.name)
      )}
    </span>
  )
}

function formatDurationShort(minutes) {
  const parsed = Number(minutes)
  if (!Number.isFinite(parsed) || parsed <= 0) return null
  const hours = Math.floor(parsed / 60)
  const mins = Math.round(parsed % 60)
  if (hours > 0) {
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
  }
  return `${mins}m`
}

function ChannelRailPanel({
  channels,
  selectedChannel,
  onSelectChannel,
  onToggleStar,
  isLoading,
  isError,
  isAdmin,
}) {
  const [search, setSearch] = useState('')

  const filteredChannels = useMemo(() => {
    if (!channels) return []
    // Starred channels float to the top; provider order is kept otherwise
    const sorted = [...channels].sort((a, b) => (b.starred ? 1 : 0) - (a.starred ? 1 : 0))
    if (!search) return sorted
    const searchLower = search.toLowerCase()
    return sorted.filter((ch) => ch.name?.toLowerCase().includes(searchLower))
  }, [channels, search])

  return (
    <div className={classes.panel}>
      <div className={classes.panelHead}>
        Channels
        {channels && <span className={classes.panelCount}>{channels.length}</span>}
      </div>
      <div className={classes.panelSearch}>
        <TextInput
          placeholder="Search channels..."
          leftSection={<IconSearch size={16} />}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <div className={classes.panelBody}>
        {isLoading ? (
          <Stack align="center" justify="center" h={200}>
            <Loader />
          </Stack>
        ) : isError ? (
          <Stack align="center" justify="center" h={200} gap="xs" px="sm">
            <IconAlertCircle size={28} color="var(--mantine-color-red-5)" />
            <Text size="sm" c="dimmed" ta="center">
              Could not reach your provider.
            </Text>
            <Text size="xs" c="dimmed" ta="center">
              {isAdmin ? (
                <>
                  Check your account status in{' '}
                  <Link to="/settings?section=accounts" style={{ color: 'var(--mantine-color-orange-5)' }}>
                    Settings → Accounts
                  </Link>
                  .
                </>
              ) : (
                'Contact your administrator to check the account connection.'
              )}
            </Text>
          </Stack>
        ) : (
          <>
            {filteredChannels.map((channel) => {
              const isActive = selectedChannel?.stream_id === channel.stream_id
              return (
                <button
                  type="button"
                  key={channel.stream_id}
                  className={`${classes.chItem}${isActive ? ` ${classes.chItemActive}` : ''}`}
                  onClick={() => onSelectChannel(channel)}
                >
                  <ChannelLogo channel={channel} />
                  <span className={classes.chName}>{channel.name}</span>
                  <ActionIcon
                    component="span"
                    variant="subtle"
                    color={channel.starred ? 'yellow' : 'gray'}
                    size="sm"
                    aria-label={channel.starred ? 'Unstar channel' : 'Star channel'}
                    title={channel.starred ? 'Unstar channel' : 'Star channel'}
                    onClick={(e) => {
                      e.stopPropagation()
                      onToggleStar?.(channel)
                    }}
                  >
                    {channel.starred ? <IconStarFilled size={16} /> : <IconStar size={16} />}
                  </ActionIcon>
                </button>
              )
            })}
            {filteredChannels.length === 0 && (
              <Text c="dimmed" ta="center" py="md" size="sm">
                {search ? 'No channels match your search.' : 'No channels available yet.'}
              </Text>
            )}
          </>
        )}
      </div>
    </div>
  )
}

const previousDownloadStatusMeta = {
  pending: { label: 'Queued', color: 'yellow' },
  downloading: { label: 'Downloading', color: 'yellow' },
  processing: { label: 'Processing', color: 'violet' },
  completed: { label: 'Downloaded', color: 'green' },
  failed: { label: 'Failed', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' },
}

function getChannelArchiveDays(channel) {
  const parsed = Number(channel?.tv_archive_duration)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null
  }
  return Math.min(365, Math.max(1, Math.trunc(parsed)))
}

function normalizeProgramTitle(value) {
  return (value || '')
    .toString()
    .trim()
    .replace(/\s+/g, ' ')
    .toLowerCase()
}

function getProgramStartMinute(program, guideOffsetHours = 0) {
  const startTime = getChannelDisplayTime(program, 'start', guideOffsetHours)
  if (startTime) {
    return Math.floor(startTime.valueOf() / 60000)
  }

  return null
}

function buildProgramSelectionKey(program, fallbackChannelId = null, guideOffsetHours = 0) {
  const channelId = (program?.channel_id ?? fallbackChannelId ?? '').toString().trim()
  const title = normalizeProgramTitle(program?.title ?? program?.program_title)
  const startMinute = getProgramStartMinute(program, guideOffsetHours)

  if (!channelId || !title || startMinute == null) {
    return null
  }

  return `${channelId}|${startMinute}|${title}`
}

export default function Browse() {
  const queryClient = useQueryClient()
  const [selectedAccountId, setSelectedAccountId] = useState(null)
  const [selectedChannel, setSelectedChannel] = useState(null)
  const [downloadProgram, setDownloadProgram] = useState(null)
  const [scheduleProgram, setScheduleProgram] = useState(null)
  const [previewTarget, setPreviewTarget] = useState(null)
  const [programSearch, setProgramSearch] = useState('')
  const [globalSearch, setGlobalSearch] = useState('')
  const [debouncedGlobalSearch] = useDebouncedValue(globalSearch, 400)
  const searchViewportRef = useRef(null)
  const searchLimit = 100
  const [browseTab, setBrowseTab] = useState('epg')
  const [vodTab, setVodTab] = useState('movies')
  const [selectedMovieCategory, setSelectedMovieCategory] = useState(null)
  const [selectedSeriesCategory, setSelectedSeriesCategory] = useState(null)
  const [movieSearch, setMovieSearch] = useState('')
  const [seriesSearch, setSeriesSearch] = useState('')
  const [selectedMovie, setSelectedMovie] = useState(null)
  const [selectedSeries, setSelectedSeries] = useState(null)
  const isMobile = useMediaQuery('(max-width: 820px)')

  const { data: authStatus } = useQuery({
    queryKey: ['auth', 'status'],
    queryFn: authApi.status,
  })

  // Fetch accounts
  const { data: accounts, isLoading: accountsLoading } = useQuery({
    queryKey: ['accounts', 'public'],
    queryFn: accountsApi.publicList,
  })

  // Fetch channels
  const {
    data: channels,
    isLoading: channelsLoading,
    isError: channelsIsError,
  } = useQuery({
    queryKey: ['channels', selectedAccountId],
    queryFn: () => channelsApi.getChannels(selectedAccountId, null, true),
    enabled: !!selectedAccountId,
    retry: false,
  })

  const toggleStarMutation = useMutation({
    mutationFn: (channel) => channelsApi.toggleStar(selectedAccountId, channel.stream_id),
    onSuccess: (data, channel) => {
      queryClient.setQueryData(['channels', selectedAccountId], (old) =>
        old?.map((ch) =>
          ch.stream_id?.toString() === channel.stream_id?.toString()
            ? { ...ch, starred: data.starred }
            : ch
        )
      )
    },
  })

  const handleToggleStar = useCallback(
    (channel) => {
      if (!selectedAccountId || toggleStarMutation.isPending) return
      toggleStarMutation.mutate(channel)
    },
    [selectedAccountId, toggleStarMutation]
  )

  // Fetch EPG for selected channel, going back as far as its catchup archive allows
  const selectedChannelArchiveDays = getChannelArchiveDays(selectedChannel)
  const { data: epgData, isLoading: epgLoading } = useQuery({
    queryKey: ['epg', selectedAccountId, selectedChannel?.stream_id, selectedChannelArchiveDays],
    queryFn: () =>
      channelsApi.getEpg(selectedAccountId, selectedChannel.stream_id, selectedChannelArchiveDays || 7, true),
    enabled: !!selectedAccountId && !!selectedChannel,
    staleTime: 0,
    refetchOnWindowFocus: false,
  })

  const {
    data: globalEpgPages,
    isLoading: globalEpgLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ['epg-search', selectedAccountId, debouncedGlobalSearch],
    queryFn: ({ pageParam = 0 }) =>
      epgApi.search(selectedAccountId, debouncedGlobalSearch, searchLimit, pageParam),
    enabled: !!selectedAccountId && debouncedGlobalSearch.length >= 2,
    retry: false,
    getNextPageParam: (lastPage, allPages) => {
      if (!lastPage || lastPage.length < searchLimit) return undefined
      return allPages.reduce((total, page) => total + page.length, 0)
    },
  })

  const globalEpgResults = useMemo(() => {
    if (!globalEpgPages?.pages) return []
    return globalEpgPages.pages.flat()
  }, [globalEpgPages])

  const { data: appSettings } = useQuery({
    queryKey: ['settings', 'public'],
    queryFn: settingsApi.getPublic,
  })
  const selectedGuideOffsetHours = useMemo(() => {
    const account = accounts?.find((entry) => Number(entry.id) === Number(selectedAccountId))
    return getGuideOffsetHours(account?.guide_offset_hours)
  }, [accounts, selectedAccountId])
  useEffect(() => {
    if (!accounts?.length) {
      if (selectedAccountId) {
        setSelectedAccountId(null)
      }
      return
    }

    const availableAccountIds = accounts.map((account) => account.id.toString())
    if (selectedAccountId && availableAccountIds.includes(selectedAccountId)) {
      return
    }

    const configuredDefaultId = appSettings?.default_account_id?.toString()
    if (configuredDefaultId && availableAccountIds.includes(configuredDefaultId)) {
      setSelectedAccountId(configuredDefaultId)
      return
    }

    setSelectedAccountId(availableAccountIds[0])
  }, [accounts, appSettings?.default_account_id, selectedAccountId])
  // The panels pin their height to the viewport, but the chrome above them
  // varies (low-disk banner, wrapping header rows), so a fixed offset in CSS
  // leaves the rail poking below the fold on small windows. Measure the real
  // top of the visible panel into --panel-top for Browse.module.css to use.
  const pageRef = useRef(null)
  useEffect(() => {
    const root = pageRef.current
    if (!root) return undefined
    const update = () => {
      const panels = root.querySelectorAll(`.${classes.panel}`)
      const visible = Array.from(panels).find((el) => el.offsetParent !== null)
      if (!visible) return
      const top = Math.round(visible.getBoundingClientRect().top + window.scrollY)
      root.style.setProperty('--panel-top', `${top}px`)
    }
    update()
    const observer = new ResizeObserver(update)
    observer.observe(document.body)
    window.addEventListener('resize', update)
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [accountsLoading])

  const { data: preferences } = useQuery({
    queryKey: ['auth', 'preferences'],
    queryFn: authApi.getPreferences,
  })
  const updatePreferencesMutation = useMutation({
    mutationFn: (value) => authApi.updatePreferences(value),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['auth', 'preferences'] })
      queryClient.invalidateQueries({ queryKey: ['settings', 'public'] })
    },
  })

  const showFuture = Boolean(preferences?.show_future_programs ?? appSettings?.show_future_programs)

  const { data: downloads = [] } = useQuery({
    queryKey: ['downloads'],
    queryFn: downloadsApi.list,
    enabled: !!selectedAccountId,
  })

  const {
    data: movieCategories = [],
    isLoading: movieCategoriesLoading,
  } = useQuery({
    queryKey: ['vod', 'movies', 'categories', selectedAccountId],
    queryFn: () => vodApi.getMovieCategories(selectedAccountId),
    enabled: !!selectedAccountId,
    retry: false,
  })

  const {
    data: seriesCategories = [],
    isLoading: seriesCategoriesLoading,
  } = useQuery({
    queryKey: ['vod', 'series', 'categories', selectedAccountId],
    queryFn: () => vodApi.getSeriesCategories(selectedAccountId),
    enabled: !!selectedAccountId,
    retry: false,
  })

  const vodAvailable = movieCategories.length > 0 || seriesCategories.length > 0

  const { data: movies = [], isLoading: moviesLoading } = useQuery({
    queryKey: ['vod', 'movies', selectedAccountId, selectedMovieCategory],
    queryFn: () => vodApi.getMovies(selectedAccountId, selectedMovieCategory),
    enabled: !!selectedAccountId && browseTab === 'vod' && vodTab === 'movies' && movieCategories.length > 0,
    retry: false,
  })

  const { data: series = [], isLoading: seriesLoading } = useQuery({
    queryKey: ['vod', 'series', selectedAccountId, selectedSeriesCategory],
    queryFn: () => vodApi.getSeries(selectedAccountId, selectedSeriesCategory),
    enabled: !!selectedAccountId && browseTab === 'vod' && vodTab === 'series' && seriesCategories.length > 0,
    retry: false,
  })

  const filteredEpgData = useMemo(() => {
    if (!epgData) return epgData
    if (!programSearch) return epgData
    const searchLower = programSearch.toLowerCase()
    return epgData.filter((program) => {
      const title = program.title?.toLowerCase() || ''
      const desc = program.description?.toLowerCase() || ''
      return title.includes(searchLower) || desc.includes(searchLower)
    })
  }, [epgData, programSearch])

  // Count for the guide header: catchup programs still inside the archive window
  const downloadableCount = useMemo(() => {
    if (!epgData) return 0
    const now = getNowUtc()
    const cutoff = selectedChannelArchiveDays ? now.subtract(selectedChannelArchiveDays, 'day') : null
    return epgData.filter((program) => {
      const end = getProgramInstant(program, 'end')
      if (!program.has_archive || !end?.isBefore(now)) return false
      return !(cutoff && end.isBefore(cutoff))
    }).length
  }, [epgData, selectedChannelArchiveDays])

  const previousDownloadLookup = useMemo(() => {
    if (!selectedAccountId) return new Map()

    const lookup = new Map()

    downloads.forEach((download) => {
      if (download?.account_id?.toString() !== selectedAccountId) {
        return
      }
      if (download?.is_vod) {
        return
      }

      const key = buildProgramSelectionKey(download, null, selectedGuideOffsetHours)
      if (!key) return

      const existing = lookup.get(key)
      if (!existing) {
        lookup.set(key, download)
        return
      }

      const existingCreatedAt = dayjs(existing.created_at)
      const nextCreatedAt = dayjs(download.created_at)

      if (!existingCreatedAt.isValid() || (nextCreatedAt.isValid() && nextCreatedAt.isAfter(existingCreatedAt))) {
        lookup.set(key, download)
      }
    })

    return lookup
  }, [downloads, selectedAccountId, selectedGuideOffsetHours])

  const getProgramPreviousDownload = useCallback(
    (program, fallbackChannelId = null) => {
      const key = buildProgramSelectionKey(program, fallbackChannelId, selectedGuideOffsetHours)
      if (!key) return null
      return previousDownloadLookup.get(key) || null
    },
    [previousDownloadLookup, selectedGuideOffsetHours]
  )

  useEffect(() => {
    if (!vodAvailable && browseTab === 'vod') {
      setBrowseTab('epg')
    }
  }, [vodAvailable, browseTab])

  useEffect(() => {
    if (vodTab === 'movies' && movieCategories.length === 0 && seriesCategories.length > 0) {
      setVodTab('series')
    }
    if (vodTab === 'series' && seriesCategories.length === 0 && movieCategories.length > 0) {
      setVodTab('movies')
    }
  }, [vodTab, movieCategories.length, seriesCategories.length])

  useEffect(() => {
    setSelectedMovieCategory(null)
    setSelectedSeriesCategory(null)
    setSelectedMovie(null)
    setSelectedSeries(null)
    setGlobalSearch('')
    setMovieSearch('')
    setSeriesSearch('')
  }, [selectedAccountId])

  useEffect(() => {
    if (searchViewportRef.current) {
      searchViewportRef.current.scrollTo({ top: 0 })
    }
  }, [debouncedGlobalSearch, browseTab])

  const handleProgramClick = (program, meta = {}) => {
    if (meta.action === 'preview') {
      setPreviewTarget({ program, mode: meta.mode || 'catchup' })
      return
    }
    if (meta.action === 'schedule') {
      setScheduleProgram(program)
      return
    }
    setDownloadProgram(program)
  }

  const handleSelectChannel = (channel) => {
    setSelectedChannel(channel)
    setProgramSearch('')
  }

  const handleBackToChannels = () => {
    setSelectedChannel(null)
    setProgramSearch('')
  }

  const handleGlobalProgramClick = async (program) => {
    if (!selectedAccountId || !program?.channel_id) {
      return
    }

    const endTime = getProgramInstant(program, 'end')
    const isPast = Boolean(endTime?.isBefore(getNowUtc()))
    if (isPast && !program.has_archive) {
      return
    }

    let channel = channels?.find(
      (ch) => ch.stream_id?.toString() === program.channel_id?.toString()
    )

    if (!channel) {
      try {
        channel = await channelsApi.getChannel(selectedAccountId, program.channel_id)
      } catch (err) {
        return
      }
    }

    if (!channel) return

    setSelectedChannel(channel)

    if (isPast && program.has_archive) {
      setDownloadProgram(program)
    } else {
      setScheduleProgram(program)
    }
  }

  const accountOptions = accounts?.map((acc) => ({
    value: acc.id.toString(),
    label: acc.name,
  })) || []

  const renderGuidePanel = ({ withPanelChrome = true } = {}) => {
    const archiveSubtitle = selectedChannelArchiveDays
      ? `${selectedChannelArchiveDays}-day catchup · ${downloadableCount} program${downloadableCount === 1 ? '' : 's'} available to download`
      : 'No catchup archive on this channel'

    const content = (
      <>
        {selectedChannel ? (
          <>
            <div className={classes.gdHead}>
              <ChannelLogo channel={selectedChannel} />
              <div className={classes.gdMeta}>
                <div className={classes.gdTitle}>{selectedChannel.name}</div>
                <div className={classes.gdSub}>{archiveSubtitle}</div>
              </div>
              <div className={classes.legend}>
                <span className={classes.legendKey}>
                  <span className={classes.swatch} style={{ background: 'var(--mantine-color-yellow-7)' }} />
                  Catchup
                </span>
                {showFuture && (
                  <span className={classes.legendKey}>
                    <span className={classes.swatch} style={{ background: 'var(--mantine-color-teal-5)' }} />
                    Upcoming
                  </span>
                )}
              </div>
            </div>
            <div className={classes.panelSearch} style={{ paddingTop: 10 }}>
              <TextInput
                placeholder="Search shows on this channel..."
                leftSection={<IconSearch size={16} />}
                value={programSearch}
                onChange={(e) => setProgramSearch(e.target.value)}
              />
            </div>
            <div className={classes.panelFill}>
              {epgLoading ? (
                <Stack align="center" justify="center" style={{ flex: 1, minHeight: 200 }}>
                  <Loader />
                </Stack>
              ) : filteredEpgData ? (
                <EPGGrid
                  epgData={filteredEpgData}
                  onProgramClick={handleProgramClick}
                  showFuture={showFuture}
                  getProgramPreviousDownload={(program) =>
                    getProgramPreviousDownload(program, selectedChannel?.stream_id)
                  }
                  guideOffsetHours={selectedGuideOffsetHours}
                  archiveDays={selectedChannelArchiveDays}
                />
              ) : (
                <Text c="dimmed" ta="center" py="xl">
                  No EPG data available
                </Text>
              )}
            </div>
          </>
        ) : (
          <Stack align="center" justify="center" style={{ flex: 1, minHeight: 0 }} px="md">
            <IconVideo size={40} opacity={0.3} />
            <Text c="dimmed" ta="center" size="sm">
              {channelsIsError ? (
                <>
                  Channel guide will appear here once your provider connection is restored.{' '}
                  {authStatus?.is_admin ? (
                    <Link to="/settings?section=accounts" style={{ color: 'var(--mantine-color-orange-5)' }}>
                      Check Settings → Accounts.
                    </Link>
                  ) : (
                    'Contact your administrator.'
                  )}
                </>
              ) : (
                'Select a channel to view its program guide.'
              )}
            </Text>
          </Stack>
        )}
      </>
    )

    if (!withPanelChrome) return content
    return <div className={classes.panel}>{content}</div>
  }

  if (accountsLoading) {
    return (
      <Stack align="center" justify="center" h={300}>
        <Loader />
      </Stack>
    )
  }

  if (!accounts?.length) {
    return (
      <Card withBorder>
        <Stack gap="xs">
          <Group gap="xs" align="flex-start">
            <IconAlertCircle size={16} />
            <div>
              <Text fw={600}>No Accounts</Text>
              <Text size="sm" c="dimmed">Add an Xtream account to start browsing and recording.</Text>
            </div>
          </Group>
          <Group gap="md" align="flex-start" wrap="wrap">
            <Stack gap={4}>
              <Button size="xs" component={Link} to="/onboarding">
                Start Setup Wizard
              </Button>
              <Text size="xs" c="dimmed">Guided walkthrough for first-time setup</Text>
            </Stack>
            <Stack gap={4}>
              <Button size="xs" variant="light" component={Link} to="/settings?section=accounts">
                Add Account
              </Button>
              <Text size="xs" c="dimmed">Already set up? Add another IPTV account</Text>
            </Stack>
          </Group>
        </Stack>
      </Card>
    )
  }

  const renderVodPanel = ({
    items,
    isLoading,
    categories,
    categoriesLoading,
    selectedCategory,
    onSelectCategory,
    search,
    onSearch,
    onSelect,
    searchPlaceholder,
    emptyLabel,
    icon: Icon,
  }) => {
    const searchLower = search.toLowerCase()
    const filteredItems = search
      ? (items || []).filter((item) => item.name?.toLowerCase().includes(searchLower))
      : items || []

    return (
      <div className={classes.panel}>
        <div className={classes.vodToolbar}>
          <TextInput
            placeholder={searchPlaceholder}
            leftSection={<IconSearch size={16} />}
            value={search}
            onChange={(e) => onSearch(e.target.value)}
            style={{ flex: 1, minWidth: 180 }}
          />
          <Select
            w={170}
            value={selectedCategory == null ? 'all' : selectedCategory.toString()}
            onChange={(value) => onSelectCategory(value === 'all' || value == null ? null : value)}
            allowDeselect={false}
            searchable
            disabled={categoriesLoading}
            aria-label="Category"
            data={[
              { value: 'all', label: 'All Categories' },
              ...(categories || []).map((category) => ({
                value: category.category_id.toString(),
                label: category.category_name,
              })),
            ]}
          />
        </div>
        <div className={classes.panelBody}>
          {isLoading ? (
            <Stack align="center" justify="center" h={300}>
              <Loader />
            </Stack>
          ) : filteredItems.length === 0 ? (
            <Stack align="center" gap="md" py={48}>
              {Icon && <Icon size={40} opacity={0.3} />}
              <Text c="dimmed" ta="center" size="sm">
                {search ? 'Nothing matches your search.' : emptyLabel}
              </Text>
            </Stack>
          ) : (
            <div className={classes.vodGrid}>
              {filteredItems.map((item) => (
                <button
                  type="button"
                  key={item.stream_id || item.series_id}
                  className={classes.vodCard}
                  onClick={() => onSelect(item)}
                >
                  <span className={classes.vodPoster}>
                    <span className={classes.vodPosterCaption}>{item.name}</span>
                    {(item.stream_icon || item.cover) && (
                      <img
                        src={item.stream_icon || item.cover}
                        alt=""
                        loading="lazy"
                        onError={(e) => {
                          e.currentTarget.style.display = 'none'
                        }}
                      />
                    )}
                  </span>
                  <span className={classes.vodName}>{item.name}</span>
                  {item.year && <span className={classes.vodYear}>{item.year}</span>}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <Stack className={classes.page} ref={pageRef}>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
        <Group gap="md" align="center" wrap="wrap" style={{ flex: 1, minWidth: 0 }}>
          <Title order={2}>Browse</Title>
          {accountOptions.length > 1 && (
            <Select
              placeholder="Select account"
              data={accountOptions}
              value={selectedAccountId}
              onChange={setSelectedAccountId}
              w={200}
              style={{ flexShrink: 0 }}
            />
          )}
        </Group>
        {browseTab === 'epg' && (
          <Switch
            label="Show future programs"
            checked={showFuture}
            onChange={(event) => updatePreferencesMutation.mutate(event.currentTarget.checked)}
            disabled={updatePreferencesMutation.isPending}
          />
        )}
      </Group>

      <Tabs value={browseTab} onChange={setBrowseTab}>
        <Tabs.List>
          <Tabs.Tab value="epg" leftSection={<IconVideo size={14} />}>
            Guide
          </Tabs.Tab>
          <Tabs.Tab value="search" leftSection={<IconSearch size={14} />}>
            Search All TV
          </Tabs.Tab>
          {vodAvailable && (
            <Tabs.Tab value="vod" leftSection={<IconPlayerPlay size={14} />}>
              On Demand
            </Tabs.Tab>
          )}
        </Tabs.List>

        <Tabs.Panel value="epg" pt="md">
          {(isMobile ?? true) ? (
            selectedChannel ? (
              <Stack gap="xs">
                <Group gap="xs">
                  <Button
                    variant="subtle"
                    size="xs"
                    leftSection={<IconChevronLeft size={14} />}
                    onClick={handleBackToChannels}
                  >
                    Channels
                  </Button>
                  <Text size="xs" c="dimmed">
                    /
                  </Text>
                  <Text size="sm" fw={500} truncate>
                    {selectedChannel?.name || 'Guide'}
                  </Text>
                </Group>
                {renderGuidePanel()}
              </Stack>
            ) : (
              <ChannelRailPanel
                channels={channels}
                selectedChannel={selectedChannel}
                onSelectChannel={handleSelectChannel}
                onToggleStar={handleToggleStar}
                isLoading={channelsLoading}
                isError={channelsIsError}
                isAdmin={authStatus?.is_admin}
              />
            )
          ) : (
            <div className={classes.layout}>
              <ChannelRailPanel
                channels={channels}
                selectedChannel={selectedChannel}
                onSelectChannel={handleSelectChannel}
                onToggleStar={handleToggleStar}
                isLoading={channelsLoading}
                isError={channelsIsError}
                isAdmin={authStatus?.is_admin}
              />
              {renderGuidePanel()}
            </div>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="search" pt="md">
          <div className={`${classes.panel} ${rowClasses.scope}`}>
            <div className={classes.panelSearch} style={{ paddingTop: 12 }}>
              <TextInput
                placeholder="Search all channels..."
                leftSection={<IconSearch size={16} />}
                value={globalSearch}
                onChange={(e) => setGlobalSearch(e.target.value)}
              />
            </div>

            {debouncedGlobalSearch.length < 2 ? (
              <Text c="dimmed" ta="center" py="md" size="sm">
                Enter at least 2 characters to search.
              </Text>
            ) : globalEpgLoading ? (
              <Stack align="center" justify="center" h={200}>
                <Loader />
              </Stack>
            ) : globalEpgResults.length === 0 ? (
              <Text c="dimmed" ta="center" py="md" size="sm">
                No matching programs found.
              </Text>
            ) : (
              <ScrollArea
                style={{ flex: 1, minHeight: 0 }}
                viewportRef={searchViewportRef}
                onScrollPositionChange={() => {
                  const viewport = searchViewportRef.current
                  if (!viewport || !hasNextPage || isFetchingNextPage) return
                  const threshold = 160
                  if (viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - threshold) {
                    fetchNextPage()
                  }
                }}
              >
                {globalEpgResults.map((program) => {
                  const end = getProgramInstant(program, 'end')
                  const isPast = Boolean(end?.isBefore(getNowUtc()))
                  const isDownloadable = isPast && program.has_archive
                  const isClickable = !isPast || isDownloadable
                  const previousDownload = getProgramPreviousDownload(program)
                  const previousStatus = previousDownload?.status
                  const previousMeta =
                    (previousStatus && previousDownloadStatusMeta[previousStatus]) || null
                  const duration = formatDurationShort(program.duration_minutes)
                  const stateClass = isDownloadable
                    ? rowClasses.downloadable
                    : isPast
                    ? rowClasses.expired
                    : rowClasses.future

                  return (
                    <div
                      key={program.epg_id || program.id}
                      className={`${rowClasses.row} ${stateClass}`}
                      onClick={() => isClickable && handleGlobalProgramClick(program)}
                    >
                      <div className={rowClasses.main}>
                        <div className={rowClasses.title}>
                          <span className={rowClasses.titleText}>{program.title}</span>
                          {previousMeta && (
                            <Badge size="xs" variant="light" color={previousMeta.color} style={{ flexShrink: 0 }}>
                              {previousMeta.label}
                            </Badge>
                          )}
                          {isPast && !isDownloadable && (
                            <Badge size="xs" variant="outline" color="gray" style={{ flexShrink: 0 }}>
                              Outside catchup
                            </Badge>
                          )}
                        </div>
                        <div className={rowClasses.chLine}>
                          <span className={rowClasses.chMini}>{channelInitials(program.channel_name)}</span>
                          {program.channel_name}
                          {' · '}
                          {formatChannelDateTime(program, 'start', selectedGuideOffsetHours, 'MMM D, h:mm A')}
                          {' – '}
                          {formatChannelDateTime(program, 'end', selectedGuideOffsetHours, 'h:mm A')}
                        </div>
                      </div>
                      <div className={rowClasses.side}>
                        {duration && <span className={rowClasses.dur}>{duration}</span>}
                        {isDownloadable && (
                          <button
                            type="button"
                            className={`${rowClasses.act} ${rowClasses.actDownload}`}
                            title="Download"
                            onClick={(e) => {
                              e.stopPropagation()
                              handleGlobalProgramClick(program)
                            }}
                          >
                            <IconDownload size={13} />
                            <span className={rowClasses.actLabel}>Download</span>
                          </button>
                        )}
                        {!isPast && (
                          <button
                            type="button"
                            className={`${rowClasses.act} ${rowClasses.actSchedule}`}
                            title="Schedule"
                            onClick={(e) => {
                              e.stopPropagation()
                              handleGlobalProgramClick(program)
                            }}
                          >
                            <IconCalendar size={13} />
                            <span className={rowClasses.actLabel}>Schedule</span>
                          </button>
                        )}
                      </div>
                    </div>
                  )
                })}
                {isFetchingNextPage && (
                  <Group justify="center" py="xs">
                    <Loader size="sm" />
                    <Text size="sm" c="dimmed">
                      Loading more results...
                    </Text>
                  </Group>
                )}
                {!hasNextPage && globalEpgResults.length > 0 && (
                  <Text size="sm" c="dimmed" ta="center" py="xs">
                    End of results.
                  </Text>
                )}
              </ScrollArea>
            )}
          </div>
        </Tabs.Panel>

        {vodAvailable && (
          <Tabs.Panel value="vod" pt="md">
            <Tabs value={vodTab} onChange={setVodTab}>
              <Tabs.List>
                {movieCategories.length > 0 && (
                  <Tabs.Tab value="movies" leftSection={<IconPlayerPlay size={14} />}>
                    Movies
                  </Tabs.Tab>
                )}
                {seriesCategories.length > 0 && (
                  <Tabs.Tab value="series" leftSection={<IconVideo size={14} />}>
                    Shows
                  </Tabs.Tab>
                )}
              </Tabs.List>

              <Tabs.Panel value="movies" pt="md">
                {renderVodPanel({
                  items: movies,
                  isLoading: moviesLoading,
                  categories: movieCategories,
                  categoriesLoading: movieCategoriesLoading,
                  selectedCategory: selectedMovieCategory,
                  onSelectCategory: setSelectedMovieCategory,
                  search: movieSearch,
                  onSearch: setMovieSearch,
                  onSelect: setSelectedMovie,
                  searchPlaceholder: 'Search movies...',
                  emptyLabel: 'No movies available in this category.',
                  icon: IconPlayerPlay,
                })}
              </Tabs.Panel>

              <Tabs.Panel value="series" pt="md">
                {renderVodPanel({
                  items: series,
                  isLoading: seriesLoading,
                  categories: seriesCategories,
                  categoriesLoading: seriesCategoriesLoading,
                  selectedCategory: selectedSeriesCategory,
                  onSelectCategory: setSelectedSeriesCategory,
                  search: seriesSearch,
                  onSearch: setSeriesSearch,
                  onSelect: setSelectedSeries,
                  searchPlaceholder: 'Search shows...',
                  emptyLabel: 'No shows available in this category.',
                  icon: IconVideo,
                })}
              </Tabs.Panel>
            </Tabs>
          </Tabs.Panel>
        )}
      </Tabs>

      <DownloadModal
        opened={!!downloadProgram}
        onClose={() => setDownloadProgram(null)}
        program={downloadProgram}
        channel={selectedChannel}
        accountId={selectedAccountId}
        guideOffsetHours={selectedGuideOffsetHours}
      />
      <ScheduleModal
        opened={!!scheduleProgram}
        onClose={() => setScheduleProgram(null)}
        program={scheduleProgram}
        channel={selectedChannel}
        accountId={selectedAccountId}
        guideOffsetHours={selectedGuideOffsetHours}
      />
      <PreviewModal
        opened={!!previewTarget}
        onClose={() => setPreviewTarget(null)}
        program={previewTarget?.program}
        channel={selectedChannel}
        accountId={selectedAccountId}
        mode={previewTarget?.mode || 'catchup'}
      />
      <MovieModal
        opened={!!selectedMovie}
        onClose={() => setSelectedMovie(null)}
        movie={selectedMovie}
        accountId={selectedAccountId}
      />
      <SeriesModal
        opened={!!selectedSeries}
        onClose={() => setSelectedSeries(null)}
        series={selectedSeries}
        accountId={selectedAccountId}
      />
    </Stack>
  )
}
