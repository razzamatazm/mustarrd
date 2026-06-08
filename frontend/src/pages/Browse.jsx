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
  Badge,
  Loader,
  Box,
  Image,
  Tabs,
  Button,
  Switch,
} from '@mantine/core'
import { useDebouncedValue, useMediaQuery, useViewportSize } from '@mantine/hooks'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import {
  IconSearch,
  IconAlertCircle,
  IconVideo,
  IconClock,
  IconPlayerPlay,
  IconChevronLeft,
} from '@tabler/icons-react'
import dayjs from 'dayjs'

import { accountsApi, authApi, channelsApi, downloadsApi, epgApi, settingsApi, vodApi } from '../api'
import EPGGrid from '../components/EPGGrid'
import DownloadModal from '../components/DownloadModal'
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

function ChannelList({ channels, selectedChannel, onSelectChannel, isLoading, isError, isAdmin }) {
  const [search, setSearch] = useState('')

  const filteredChannels = useMemo(() => {
    if (!channels) return []
    if (!search) return channels
    const searchLower = search.toLowerCase()
    return channels.filter((ch) => ch.name?.toLowerCase().includes(searchLower))
  }, [channels, search])

  if (isLoading) {
    return (
      <Stack align="center" justify="center" h={200}>
        <Loader />
      </Stack>
    )
  }

  if (isError) {
    return (
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
    )
  }

  return (
    <Stack gap="xs" style={{ height: '100%' }}>
      <TextInput
        placeholder="Search channels..."
        leftSection={<IconSearch size={16} />}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <ScrollArea style={{ flex: 1 }}>
        <Stack gap={4}>
          {filteredChannels.map((channel) => (
            <Card
              key={channel.stream_id}
              padding="xs"
              radius="sm"
              withBorder
              style={{
                cursor: 'pointer',
                backgroundColor:
                  selectedChannel?.stream_id === channel.stream_id
                    ? 'rgba(245, 159, 0, 0.1)'
                    : undefined,
              }}
              onClick={() => onSelectChannel(channel)}
            >
              <Group gap="xs" wrap="nowrap">
                {channel.stream_icon ? (
                  <Image
                    src={channel.stream_icon}
                    alt={channel.name}
                    w={32}
                    h={32}
                    fit="contain"
                    fallbackSrc="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>"
                  />
                ) : (
                  <Box w={32} h={32} bg="dark.5" style={{ borderRadius: 4 }} />
                )}
                <Stack gap={0} style={{ flex: 1, overflow: 'hidden' }}>
                  <Text size="sm" fw={500} truncate>
                    {channel.name}
                  </Text>
                  {channel.tv_archive_duration && (
                    <Text size="xs" c="dimmed">
                      {channel.tv_archive_duration} days catchup
                    </Text>
                  )}
                </Stack>
              </Group>
            </Card>
          ))}
          {filteredChannels.length === 0 && (
            <Text c="dimmed" ta="center" py="md">
              {search ? 'No channels match your search.' : 'No channels available yet.'}
            </Text>
          )}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}

function CategoryList({ categories, selectedCategory, onSelectCategory, isLoading, label }) {
  const [search, setSearch] = useState('')

  const filtered = useMemo(() => {
    if (!categories) return []
    if (!search) return categories
    const searchLower = search.toLowerCase()
    return categories.filter((cat) => cat.category_name?.toLowerCase().includes(searchLower))
  }, [categories, search])

  if (isLoading) {
    return (
      <Stack align="center" justify="center" h={200}>
        <Loader />
      </Stack>
    )
  }

  return (
    <Stack gap="xs" style={{ height: '100%' }}>
      <TextInput
        placeholder={`Search ${label || 'categories'}...`}
        leftSection={<IconSearch size={16} />}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <ScrollArea style={{ flex: 1 }}>
        <Stack gap={4}>
          <Card
            padding="xs"
            radius="sm"
            withBorder
            style={{
              cursor: 'pointer',
              backgroundColor: selectedCategory == null ? 'rgba(245, 159, 0, 0.1)' : undefined,
            }}
            onClick={() => onSelectCategory(null)}
          >
            <Text size="sm" fw={500}>
              All Categories
            </Text>
          </Card>
          {filtered.map((category) => (
            <Card
              key={category.category_id}
              padding="xs"
              radius="sm"
              withBorder
              style={{
                cursor: 'pointer',
                backgroundColor:
                  selectedCategory === category.category_id
                    ? 'rgba(245, 159, 0, 0.1)'
                    : undefined,
              }}
              onClick={() => onSelectCategory(category.category_id)}
            >
              <Text size="sm" fw={500} truncate>
                {category.category_name}
              </Text>
            </Card>
          ))}
          {filtered.length === 0 && (
            <Text c="dimmed" ta="center" py="md">
              {search ? 'No categories match your search.' : 'No categories available.'}
            </Text>
          )}
        </Stack>
      </ScrollArea>
    </Stack>
  )
}

function VodGrid({ items, onSelect, isLoading, searchPlaceholder, emptyLabel, icon: Icon }) {
  const [search, setSearch] = useState('')

  const filteredItems = useMemo(() => {
    if (!items) return []
    if (!search) return items
    const searchLower = search.toLowerCase()
    return items.filter((item) => item.name?.toLowerCase().includes(searchLower))
  }, [items, search])

  if (isLoading) {
    return (
      <Stack align="center" justify="center" h={300}>
        <Loader />
      </Stack>
    )
  }

  return (
    <Stack gap="xs" style={{ height: '100%' }}>
      <TextInput
        placeholder={searchPlaceholder}
        leftSection={<IconSearch size={16} />}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      {filteredItems.length === 0 ? (
        <Card shadow="sm" padding="xl" radius="md" withBorder>
          <Stack align="center" gap="md">
            {Icon && <Icon size={48} opacity={0.3} />}
            <Text c="dimmed" ta="center">
              {emptyLabel}
            </Text>
          </Stack>
        </Card>
      ) : (
        <ScrollArea style={{ flex: 1 }}>
          <Stack gap="sm">
            {filteredItems.map((item) => (
              <Card
                key={item.stream_id || item.series_id}
                padding="sm"
                radius="md"
                withBorder
                style={{ cursor: 'pointer' }}
                onClick={() => onSelect(item)}
              >
                <Group gap="md" wrap="nowrap">
                  {item.stream_icon || item.cover ? (
                    <Image
                      src={item.stream_icon || item.cover}
                      alt={item.name}
                      w={64}
                      h={64}
                      fit="contain"
                      fallbackSrc="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>"
                    />
                  ) : (
                    <Box w={64} h={64} bg="dark.5" style={{ borderRadius: 6 }} />
                  )}
                  <Stack gap={2} style={{ flex: 1, overflow: 'hidden' }}>
                    <Text fw={500} truncate>
                      {item.name}
                    </Text>
                    {item.year && (
                      <Text size="xs" c="dimmed">
                        {item.year}
                      </Text>
                    )}
                  </Stack>
                  <Button variant="light" size="xs">
                    View
                  </Button>
                </Group>
              </Card>
            ))}
          </Stack>
        </ScrollArea>
      )}
    </Stack>
  )
}

function getInitialDesktopPanelHeight() {
  if (typeof window === 'undefined') {
    return 520
  }
  return Math.max(360, Math.floor(window.innerHeight - 180))
}

function formatArchiveDuration(days) {
  const parsed = Number(days)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return 'No archive'
  }
  return `${Math.floor(parsed)} days`
}

const previousDownloadStatusMeta = {
  pending: { label: 'Queued', color: 'yellow' },
  downloading: { label: 'Downloading', color: 'yellow' },
  processing: { label: 'Processing', color: 'violet' },
  completed: { label: 'Downloaded', color: 'green' },
  failed: { label: 'Failed', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' },
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
  const [programSearch, setProgramSearch] = useState('')
  const [globalSearch, setGlobalSearch] = useState('')
  const [debouncedGlobalSearch] = useDebouncedValue(globalSearch, 400)
  const searchViewportRef = useRef(null)
  const desktopEpgLayoutRef = useRef(null)
  const desktopSearchPanelRef = useRef(null)
  const desktopMoviesLayoutRef = useRef(null)
  const desktopSeriesLayoutRef = useRef(null)
  const searchLimit = 100
  const [browseTab, setBrowseTab] = useState('epg')
  const [vodTab, setVodTab] = useState('movies')
  const [selectedMovieCategory, setSelectedMovieCategory] = useState(null)
  const [selectedSeriesCategory, setSelectedSeriesCategory] = useState(null)
  const [selectedMovie, setSelectedMovie] = useState(null)
  const [selectedSeries, setSelectedSeries] = useState(null)
  const isMobile = useMediaQuery('(max-width: 48em)')
  const { height: viewportHeight } = useViewportSize()
  const [desktopEpgHeight, setDesktopEpgHeight] = useState(getInitialDesktopPanelHeight)
  const [desktopSearchHeight, setDesktopSearchHeight] = useState(getInitialDesktopPanelHeight)
  const [desktopMoviesHeight, setDesktopMoviesHeight] = useState(getInitialDesktopPanelHeight)
  const [desktopSeriesHeight, setDesktopSeriesHeight] = useState(getInitialDesktopPanelHeight)
  const [mobileMoviesView, setMobileMoviesView] = useState('categories')
  const [mobileSeriesView, setMobileSeriesView] = useState('categories')

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
  })

  // Fetch EPG for selected channel
  const { data: epgData, isLoading: epgLoading } = useQuery({
    queryKey: ['epg', selectedAccountId, selectedChannel?.stream_id],
    queryFn: () => channelsApi.getEpg(selectedAccountId, selectedChannel.stream_id, 7, true),
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
    setMobileMoviesView('categories')
    setMobileSeriesView('categories')
  }, [selectedAccountId])

  useEffect(() => {
    if (searchViewportRef.current) {
      searchViewportRef.current.scrollTo({ top: 0 })
    }
  }, [debouncedGlobalSearch, browseTab])

  useEffect(() => {
    if (isMobile) {
      return
    }

    const measuredViewportHeight =
      viewportHeight ||
      (typeof window !== 'undefined' ? window.innerHeight || document.documentElement.clientHeight : 0)

    if (!measuredViewportHeight) {
      return
    }

    const frame = window.requestAnimationFrame(() => {
      const resolveHeight = (node) => {
        if (!node) return null
        const layoutTop = node.getBoundingClientRect().top
        if (!Number.isFinite(layoutTop) || layoutTop >= measuredViewportHeight) return null
        const bottomPadding = 16
        return Math.max(360, Math.floor(measuredViewportHeight - layoutTop - bottomPadding))
      }

      if (browseTab === 'epg') {
        const nextHeight = resolveHeight(desktopEpgLayoutRef.current)
        if (nextHeight != null) {
          setDesktopEpgHeight((previous) => (previous === nextHeight ? previous : nextHeight))
        }
      }

      if (browseTab === 'search') {
        const nextHeight = resolveHeight(desktopSearchPanelRef.current)
        if (nextHeight != null) {
          setDesktopSearchHeight((previous) => (previous === nextHeight ? previous : nextHeight))
        }
      }

      if (browseTab === 'vod' && vodTab === 'movies') {
        const nextHeight = resolveHeight(desktopMoviesLayoutRef.current)
        if (nextHeight != null) {
          setDesktopMoviesHeight((previous) => (previous === nextHeight ? previous : nextHeight))
        }
      }

      if (browseTab === 'vod' && vodTab === 'series') {
        const nextHeight = resolveHeight(desktopSeriesLayoutRef.current)
        if (nextHeight != null) {
          setDesktopSeriesHeight((previous) => (previous === nextHeight ? previous : nextHeight))
        }
      }
    })

    return () => window.cancelAnimationFrame(frame)
  }, [browseTab, vodTab, isMobile, viewportHeight])

  const handleProgramClick = (program, meta = {}) => {
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

  const handleSelectMovie = (movie) => {
    setSelectedMovie(movie)
  }

  const handleSelectSeries = (seriesItem) => {
    setSelectedSeries(seriesItem)
  }

  const handleSelectMovieCategory = (categoryId) => {
    setSelectedMovieCategory(categoryId)
    if (isMobile) {
      setMobileMoviesView('items')
    }
  }

  const handleSelectSeriesCategory = (categoryId) => {
    setSelectedSeriesCategory(categoryId)
    if (isMobile) {
      setMobileSeriesView('items')
    }
  }

  const handleBackToMovieCategories = () => {
    setMobileMoviesView('categories')
  }

  const handleBackToSeriesCategories = () => {
    setMobileSeriesView('categories')
  }

  const movieCategoryLabel = useMemo(() => {
    if (!selectedMovieCategory) return 'All Categories'
    const match = movieCategories.find((category) => category.category_id === selectedMovieCategory)
    return match?.category_name || 'Selected Category'
  }, [movieCategories, selectedMovieCategory])

  const seriesCategoryLabel = useMemo(() => {
    if (!selectedSeriesCategory) return 'All Categories'
    const match = seriesCategories.find((category) => category.category_id === selectedSeriesCategory)
    return match?.category_name || 'Selected Category'
  }, [seriesCategories, selectedSeriesCategory])

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

  return (
    <Stack>
      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
        <Group gap="md" align="center" wrap="wrap" style={{ flex: 1, minWidth: 0 }}>
          <Title order={2}>
            {browseTab === 'vod'
              ? 'Browse On Demand'
              : browseTab === 'search'
              ? 'Search All TV'
              : 'Browse EPG'}
          </Title>
          {browseTab === 'epg' && (
            <Switch
              label="Show Future Programs"
              checked={Boolean(preferences?.show_future_programs ?? appSettings?.show_future_programs)}
              onChange={(event) => updatePreferencesMutation.mutate(event.currentTarget.checked)}
              disabled={updatePreferencesMutation.isPending}
            />
          )}
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
      </Group>

      <Tabs value={browseTab} onChange={setBrowseTab}>
        <Tabs.List>
          <Tabs.Tab value="epg" leftSection={<IconVideo size={14} />}>
            Browse EPG
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
              <Card shadow="sm" padding="md" radius="md" withBorder>
                <Stack>
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
                      {selectedChannel?.name || 'EPG'}
                    </Text>
                  </Group>
                  <Stack style={{ height: '100%' }}>
                    <Group justify="space-between">
                      <Group gap="xs">
                        {selectedChannel?.stream_icon ? (
                          <Image
                            src={selectedChannel.stream_icon}
                            alt={selectedChannel.name}
                            w={32}
                            h={32}
                            fit="contain"
                          />
                        ) : (
                          <IconVideo size={28} opacity={0.4} />
                        )}
                        <Text fw={500}>
                          {selectedChannel ? selectedChannel.name : 'No channel selected'}
                        </Text>
                      </Group>
                      {selectedChannel && (
                        <Badge variant="light" leftSection={<IconClock size={12} />}>
                          {formatArchiveDuration(selectedChannel.tv_archive_duration)}
                        </Badge>
                      )}
                    </Group>

                    {selectedChannel ? (
                      <Stack style={{ flex: 1, minHeight: 0 }}>
                        <TextInput
                          placeholder="Search shows on this channel..."
                          leftSection={<IconSearch size={16} />}
                          value={programSearch}
                          onChange={(e) => setProgramSearch(e.target.value)}
                          mb="md"
                        />
                        {epgLoading ? (
                          <Stack align="center" justify="center" h={300}>
                            <Loader />
                          </Stack>
                        ) : filteredEpgData ? (
                          <EPGGrid
                            epgData={filteredEpgData}
                            onProgramClick={handleProgramClick}
                            showFuture={Boolean(preferences?.show_future_programs ?? appSettings?.show_future_programs)}
                            getProgramPreviousDownload={(program) =>
                              getProgramPreviousDownload(program, selectedChannel?.stream_id)
                            }
                            guideOffsetHours={selectedGuideOffsetHours}
                          />
                        ) : (
                          <Text c="dimmed" ta="center" py="xl">
                            No EPG data available
                          </Text>
                        )}
                      </Stack>
                    ) : (
                      <Stack align="center" justify="center" h={300}>
                        <IconVideo size={48} opacity={0.3} />
                        <Text c="dimmed">Select a channel to view its EPG</Text>
                      </Stack>
                    )}
                  </Stack>
                </Stack>
              </Card>
            ) : (
              <Card shadow="sm" padding="md" radius="md" withBorder>
                <Stack gap="xs">
                  <Group gap="xs">
                    <IconVideo size={18} />
                    <Text fw={500}>Channels</Text>
                    {channels && (
                      <Badge size="sm" variant="light">
                        {channels.length}
                      </Badge>
                    )}
                  </Group>
                  <ChannelList
                    channels={channels}
                    selectedChannel={selectedChannel}
                    onSelectChannel={handleSelectChannel}
                    isLoading={channelsLoading}
                    isError={channelsIsError}
                    isAdmin={authStatus?.is_admin}
                  />
                </Stack>
              </Card>
            )
          ) : (
            <div
              ref={desktopEpgLayoutRef}
              style={{
                display: 'grid',
                gridTemplateColumns: '300px 1fr',
                gap: 16,
                alignItems: 'stretch',
                height: `${desktopEpgHeight}px`,
              }}
            >
              <Card shadow="sm" padding="md" radius="md" withBorder style={{ height: '100%' }}>
                <Stack gap="xs" style={{ height: '100%' }}>
                  <Group gap="xs">
                    <IconVideo size={18} />
                    <Text fw={500}>Channels</Text>
                    {channels && (
                      <Badge size="sm" variant="light">
                        {channels.length}
                      </Badge>
                    )}
                  </Group>
                  <ChannelList
                    channels={channels}
                    selectedChannel={selectedChannel}
                    onSelectChannel={handleSelectChannel}
                    isLoading={channelsLoading}
                    isError={channelsIsError}
                    isAdmin={authStatus?.is_admin}
                  />
                </Stack>
              </Card>

              <Card shadow="sm" padding="md" radius="md" withBorder style={{ height: '100%' }}>
                <Stack style={{ height: '100%' }}>
                  <Group justify="space-between">
                    <Group gap="xs">
                      {selectedChannel?.stream_icon ? (
                        <Image
                          src={selectedChannel.stream_icon}
                          alt={selectedChannel.name}
                          w={32}
                          h={32}
                          fit="contain"
                        />
                      ) : (
                        <IconVideo size={28} opacity={0.4} />
                      )}
                      <Text fw={500}>
                        {selectedChannel ? selectedChannel.name : 'No channel selected'}
                      </Text>
                    </Group>
                    {selectedChannel && (
                      <Badge variant="light" leftSection={<IconClock size={12} />}>
                        {formatArchiveDuration(selectedChannel.tv_archive_duration)}
                      </Badge>
                    )}
                  </Group>

                  {selectedChannel ? (
                    <Stack style={{ flex: 1, minHeight: 0 }}>
                      <TextInput
                        placeholder="Search shows on this channel..."
                        leftSection={<IconSearch size={16} />}
                        value={programSearch}
                        onChange={(e) => setProgramSearch(e.target.value)}
                        mb="md"
                      />
                        {epgLoading ? (
                          <Stack align="center" justify="center" style={{ flex: 1, minHeight: 0 }}>
                            <Loader />
                          </Stack>
                        ) : filteredEpgData ? (
                        <EPGGrid
                          epgData={filteredEpgData}
                          onProgramClick={handleProgramClick}
                          showFuture={Boolean(preferences?.show_future_programs ?? appSettings?.show_future_programs)}
                          getProgramPreviousDownload={(program) =>
                            getProgramPreviousDownload(program, selectedChannel?.stream_id)
                          }
                          guideOffsetHours={selectedGuideOffsetHours}
                        />
                      ) : (
                        <Text c="dimmed" ta="center" py="xl">
                          No EPG data available
                        </Text>
                      )}
                    </Stack>
                  ) : channelsIsError ? null : (
                    <Stack align="center" justify="center" style={{ flex: 1, minHeight: 0 }}>
                      <IconVideo size={48} opacity={0.3} />
                      <Text c="dimmed">Select a channel to view its EPG</Text>
                    </Stack>
                  )}
                </Stack>
              </Card>
            </div>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="search" pt="md">
          <Card
            ref={desktopSearchPanelRef}
            shadow="sm"
            padding="md"
            radius="md"
            withBorder
            style={{ height: isMobile ? 'auto' : `${desktopSearchHeight}px` }}
          >
            <Stack style={{ height: '100%', minHeight: 0 }}>
              <TextInput
                placeholder="Search all channels..."
                leftSection={<IconSearch size={16} />}
                value={globalSearch}
                onChange={(e) => setGlobalSearch(e.target.value)}
              />

              {debouncedGlobalSearch.length < 2 ? (
                <Text c="dimmed" ta="center" py="md">
                  Enter at least 2 characters to search.
                </Text>
              ) : globalEpgLoading ? (
                <Stack align="center" justify="center" h={200}>
                  <Loader />
                </Stack>
              ) : globalEpgResults.length === 0 ? (
                <Text c="dimmed" ta="center" py="md">
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
                  <Stack gap="xs">
                    {globalEpgResults.map((program) => {
                      const end = getProgramInstant(program, 'end')
                      const isPast = Boolean(end?.isBefore(getNowUtc()))
                      const isDownloadable = isPast && program.has_archive
                      const isClickable = !isPast || isDownloadable
                      const actionLabel = isDownloadable ? 'Download' : isPast ? 'Unavailable' : 'Schedule'
                      const previousDownload = getProgramPreviousDownload(program)
                      const previousStatus = previousDownload?.status
                      const previousMeta =
                        (previousStatus && previousDownloadStatusMeta[previousStatus]) || null
                      return (
                        <Card
                          key={program.epg_id || program.id}
                          padding="sm"
                          radius="sm"
                          withBorder
                          style={{
                            cursor: isClickable ? 'pointer' : 'default',
                            opacity: isClickable ? 1 : 0.6,
                          }}
                          onClick={() => isClickable && handleGlobalProgramClick(program)}
                        >
                          <Stack gap={4}>
                            <Group justify="space-between" wrap="nowrap">
                              <Text
                                size={isMobile ? 'xs' : 'sm'}
                                fw={500}
                                lineClamp={isMobile ? 2 : 1}
                                style={{ flex: 1, minWidth: 0, lineHeight: isMobile ? 1.25 : undefined }}
                              >
                                {program.title}
                              </Text>
                              <Group gap={6} wrap="nowrap" style={{ flexShrink: 0 }}>
                                {previousMeta && (
                                  <Badge size="xs" variant="light" color={previousMeta.color}>
                                    {previousMeta.label}
                                  </Badge>
                                )}
                                <Badge
                                  size="xs"
                                  variant="light"
                                  color={isDownloadable ? 'yellow' : isPast ? 'gray' : 'teal'}
                                >
                                  {actionLabel}
                                </Badge>
                              </Group>
                            </Group>
                            <Text size="xs" c="dimmed">
                              {program.channel_name}
                            </Text>
                            <Group gap="xs">
                              <Badge size="xs" variant="outline">
                                {formatChannelDateTime(program, 'start', selectedGuideOffsetHours, 'MMM D, h:mm A')}
                                {' - '}
                                {formatChannelDateTime(program, 'end', selectedGuideOffsetHours, 'h:mm A')}
                              </Badge>
                              <Badge size="xs" variant="light">
                                {program.duration_minutes}m
                              </Badge>
                            </Group>
                          </Stack>
                        </Card>
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
                  </Stack>
                </ScrollArea>
              )}
            </Stack>
          </Card>
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
                {isMobile ? (
                  mobileMoviesView === 'categories' ? (
                    <Card shadow="sm" padding="md" radius="md" withBorder>
                      <Stack gap="xs">
                        <Group gap="xs">
                          <IconPlayerPlay size={18} />
                          <Text fw={500}>Movie Categories</Text>
                          {movieCategories && (
                            <Badge size="sm" variant="light">
                              {movieCategories.length}
                            </Badge>
                          )}
                        </Group>
                        <CategoryList
                          categories={movieCategories}
                          selectedCategory={selectedMovieCategory}
                          onSelectCategory={handleSelectMovieCategory}
                          isLoading={movieCategoriesLoading}
                          label="movie categories"
                        />
                      </Stack>
                    </Card>
                  ) : (
                    <Card shadow="sm" padding="md" radius="md" withBorder>
                      <Stack gap="md">
                        <Group gap="xs">
                          <Button
                            variant="subtle"
                            size="xs"
                            leftSection={<IconChevronLeft size={14} />}
                            onClick={handleBackToMovieCategories}
                          >
                            Categories
                          </Button>
                          <Text size="xs" c="dimmed">
                            /
                          </Text>
                          <Text size="sm" fw={500} truncate>
                            {movieCategoryLabel}
                          </Text>
                        </Group>
                        <VodGrid
                          items={movies}
                          onSelect={handleSelectMovie}
                          isLoading={moviesLoading}
                          searchPlaceholder="Search movies..."
                          emptyLabel="No movies available in this category."
                          icon={IconPlayerPlay}
                        />
                      </Stack>
                    </Card>
                  )
                ) : (
                  <div
                    ref={desktopMoviesLayoutRef}
                    style={{
                      display: 'grid',
                      gridTemplateColumns: '300px 1fr',
                      gap: 16,
                      alignItems: 'stretch',
                      height: `${desktopMoviesHeight}px`,
                    }}
                  >
                    <Card shadow="sm" padding="md" radius="md" withBorder style={{ height: '100%' }}>
                      <Stack gap="xs" style={{ height: '100%' }}>
                        <Group gap="xs">
                          <IconPlayerPlay size={18} />
                          <Text fw={500}>Movie Categories</Text>
                          {movieCategories && (
                            <Badge size="sm" variant="light">
                              {movieCategories.length}
                            </Badge>
                          )}
                        </Group>
                        <CategoryList
                          categories={movieCategories}
                          selectedCategory={selectedMovieCategory}
                          onSelectCategory={setSelectedMovieCategory}
                          isLoading={movieCategoriesLoading}
                          label="movie categories"
                        />
                      </Stack>
                    </Card>

                    <Card shadow="sm" padding="md" radius="md" withBorder style={{ height: '100%' }}>
                      <VodGrid
                        items={movies}
                        onSelect={handleSelectMovie}
                        isLoading={moviesLoading}
                        searchPlaceholder="Search movies..."
                        emptyLabel="No movies available in this category."
                        icon={IconPlayerPlay}
                      />
                    </Card>
                  </div>
                )}
              </Tabs.Panel>

              <Tabs.Panel value="series" pt="md">
                {isMobile ? (
                  mobileSeriesView === 'categories' ? (
                    <Card shadow="sm" padding="md" radius="md" withBorder>
                      <Stack gap="xs">
                        <Group gap="xs">
                          <IconVideo size={18} />
                          <Text fw={500}>Show Categories</Text>
                          {seriesCategories && (
                            <Badge size="sm" variant="light">
                              {seriesCategories.length}
                            </Badge>
                          )}
                        </Group>
                        <CategoryList
                          categories={seriesCategories}
                          selectedCategory={selectedSeriesCategory}
                          onSelectCategory={handleSelectSeriesCategory}
                          isLoading={seriesCategoriesLoading}
                          label="show categories"
                        />
                      </Stack>
                    </Card>
                  ) : (
                    <Card shadow="sm" padding="md" radius="md" withBorder>
                      <Stack gap="md">
                        <Group gap="xs">
                          <Button
                            variant="subtle"
                            size="xs"
                            leftSection={<IconChevronLeft size={14} />}
                            onClick={handleBackToSeriesCategories}
                          >
                            Categories
                          </Button>
                          <Text size="xs" c="dimmed">
                            /
                          </Text>
                          <Text size="sm" fw={500} truncate>
                            {seriesCategoryLabel}
                          </Text>
                        </Group>
                        <VodGrid
                          items={series}
                          onSelect={handleSelectSeries}
                          isLoading={seriesLoading}
                          searchPlaceholder="Search shows..."
                          emptyLabel="No shows available in this category."
                          icon={IconVideo}
                        />
                      </Stack>
                    </Card>
                  )
                ) : (
                  <div
                    ref={desktopSeriesLayoutRef}
                    style={{
                      display: 'grid',
                      gridTemplateColumns: '300px 1fr',
                      gap: 16,
                      alignItems: 'stretch',
                      height: `${desktopSeriesHeight}px`,
                    }}
                  >
                    <Card shadow="sm" padding="md" radius="md" withBorder style={{ height: '100%' }}>
                      <Stack gap="xs" style={{ height: '100%' }}>
                        <Group gap="xs">
                          <IconVideo size={18} />
                          <Text fw={500}>Show Categories</Text>
                          {seriesCategories && (
                            <Badge size="sm" variant="light">
                              {seriesCategories.length}
                            </Badge>
                          )}
                        </Group>
                        <CategoryList
                          categories={seriesCategories}
                          selectedCategory={selectedSeriesCategory}
                          onSelectCategory={setSelectedSeriesCategory}
                          isLoading={seriesCategoriesLoading}
                          label="show categories"
                        />
                      </Stack>
                    </Card>

                    <Card shadow="sm" padding="md" radius="md" withBorder style={{ height: '100%' }}>
                      <VodGrid
                        items={series}
                        onSelect={handleSelectSeries}
                        isLoading={seriesLoading}
                        searchPlaceholder="Search shows..."
                        emptyLabel="No shows available in this category."
                        icon={IconVideo}
                      />
                    </Card>
                  </div>
                )}
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
