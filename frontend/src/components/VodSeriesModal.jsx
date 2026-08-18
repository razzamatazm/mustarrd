import { useEffect, useMemo, useState } from 'react'
import {
  ActionIcon,
  Modal,
  Stack,
  Text,
  Button,
  Group,
  Badge,
  Loader,
  Alert,
  Image,
  Checkbox,
  Accordion,
  ScrollArea,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { IconVideo, IconListCheck, IconEye } from '@tabler/icons-react'

import { vodApi } from '../api'
import VodPreviewModal from './VodPreviewModal'

function normalizeEpisode(raw, seasonFallback = 0) {
  return {
    id: raw.id?.toString() || raw.stream_id?.toString() || raw.episode_id?.toString(),
    title: raw.title || raw.name || '',
    episode_num: Number(raw.episode_num || raw.episode || 0),
    season: Number(raw.season || seasonFallback || 0),
    container_extension: raw.container_extension || raw.container_extension_name || raw.container || null,
    direct_source: raw.direct_source || null,
    duration_minutes: raw.info?.duration_secs
      ? Math.round(Number(raw.info.duration_secs) / 60)
      : raw.info?.duration
      ? Math.round(Number(raw.info.duration) / 60)
      : null,
  }
}

export default function SeriesModal({ opened, onClose, series, accountId }) {
  const queryClient = useQueryClient()
  const [selectedIds, setSelectedIds] = useState(new Set())
  // The preview belongs on the episode, not on the show: you check whether
  // this episode is the thing you meant to download.
  const [previewEpisode, setPreviewEpisode] = useState(null)

  const { data: seriesInfo, isLoading, error } = useQuery({
    queryKey: ['vod', 'series', accountId, series?.series_id],
    queryFn: () => vodApi.getSeriesInfo(accountId, series.series_id),
    enabled: !!opened && !!series && !!accountId,
    retry: false,
  })

  useEffect(() => {
    if (opened) {
      setSelectedIds(new Set())
      setPreviewEpisode(null)
    }
  }, [opened, series?.series_id])

  const seasons = useMemo(() => {
    const episodesBySeason = seriesInfo?.episodes || {}
    return Object.entries(episodesBySeason)
      .map(([seasonKey, episodes]) => {
        const seasonNum = Number(seasonKey) || episodes?.[0]?.season || 0
        const normalized = (episodes || []).map((ep) => normalizeEpisode(ep, seasonNum))
        return {
          season: seasonNum,
          episodes: normalized.filter((ep) => ep.id),
        }
      })
      .filter((season) => season.episodes.length > 0)
      .sort((a, b) => a.season - b.season)
  }, [seriesInfo])

  const allEpisodeIds = useMemo(() => {
    return seasons.flatMap((season) => season.episodes.map((ep) => ep.id))
  }, [seasons])

  const selectedCount = selectedIds.size
  const allSelected = allEpisodeIds.length > 0 && selectedCount === allEpisodeIds.length

  const toggleEpisode = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
  }

  const toggleSeason = (season) => {
    const seasonIds = season.episodes.map((ep) => ep.id)
    setSelectedIds((prev) => {
      const next = new Set(prev)
      const shouldSelect = seasonIds.some((id) => !next.has(id))
      seasonIds.forEach((id) => {
        if (shouldSelect) {
          next.add(id)
        } else {
          next.delete(id)
        }
      })
      return next
    })
  }

  const toggleAll = () => {
    setSelectedIds((prev) => {
      if (allSelected) {
        return new Set()
      }
      return new Set(allEpisodeIds)
    })
  }

  const downloadMutation = useMutation({
    mutationFn: (data) => vodApi.downloadSeries(data),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'Downloads Queued',
        message: `Queued ${data.count} episode${data.count === 1 ? '' : 's'} for download`,
        color: 'green',
      })
      onClose()
    },
    onError: (error) => {
      notifications.show({
        title: 'Error',
        message: error.message,
        color: 'red',
      })
    },
  })

  const handleDownload = () => {
    if (!series || !accountId || selectedIds.size === 0) return

    const episodes = seasons
      .flatMap((season) => season.episodes)
      .filter((episode) => selectedIds.has(episode.id))
      .map((episode) => ({
        id: episode.id,
        season: episode.season,
        episode_num: episode.episode_num,
        title: episode.title,
        container_extension: episode.container_extension,
        direct_source: episode.direct_source,
        duration_minutes: episode.duration_minutes,
      }))

    downloadMutation.mutate({
      account_id: parseInt(accountId),
      series_id: series.series_id?.toString(),
      series_name: series.name,
      episodes,
    })
  }

  if (!series) return null

  return (
    <>
      <Modal opened={opened} onClose={onClose} title="On Demand Show" size="xl">
        <Stack>
          <Group gap="md" wrap="nowrap">
            {series.cover && (
              <Image
                src={series.cover}
                alt={series.name}
                w={96}
                h={96}
                fit="contain"
                fallbackSrc="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'/>"
              />
            )}
            <Stack gap={4} style={{ flex: 1 }}>
              <Text fw={600} size="lg">
                {series.name}
              </Text>
              <Badge variant="light" leftSection={<IconVideo size={12} />}>
                Show
              </Badge>
            </Stack>
            <Button variant="light" onClick={toggleAll} leftSection={<IconListCheck size={14} />}>
              {allSelected ? 'Clear All' : 'Select All'}
            </Button>
          </Group>

          {isLoading ? (
            <Group>
              <Loader size="sm" />
              <Text size="sm" c="dimmed">Loading episodes...</Text>
            </Group>
          ) : error ? (
            <Alert color="red" variant="light">
              <Text size="sm">Failed to load episodes for this show.</Text>
            </Alert>
          ) : seasons.length === 0 ? (
            <Alert color="yellow" variant="light">
              <Text size="sm">No episodes available for download.</Text>
            </Alert>
          ) : (
            <ScrollArea h={420} offsetScrollbars>
              <Accordion variant="separated" multiple>
                {seasons.map((season) => {
                  const seasonIds = season.episodes.map((ep) => ep.id)
                  const seasonSelected = seasonIds.every((id) => selectedIds.has(id))
                  const seasonPartial = seasonIds.some((id) => selectedIds.has(id)) && !seasonSelected

                  return (
                    <Accordion.Item key={`season-${season.season}`} value={`season-${season.season}`}>
                      <Accordion.Control>
                        <Group justify="space-between" wrap="nowrap">
                          <Group gap="sm">
                            <Checkbox
                              checked={seasonSelected}
                              indeterminate={seasonPartial}
                              onChange={() => toggleSeason(season)}
                              onClick={(e) => e.stopPropagation()}
                            />
                            <Text fw={500}>Season {season.season.toString().padStart(2, '0')}</Text>
                          </Group>
                          <Text size="xs" c="dimmed">
                            {season.episodes.length} episodes
                          </Text>
                        </Group>
                      </Accordion.Control>
                      <Accordion.Panel>
                        <Stack gap="xs">
                          {season.episodes.map((episode) => (
                            <Group key={episode.id} gap="sm" wrap="nowrap">
                              <Checkbox
                                checked={selectedIds.has(episode.id)}
                                onChange={() => toggleEpisode(episode.id)}
                              />
                              <Text size="sm" c="dimmed">
                                S{episode.season.toString().padStart(2, '0')}E{episode.episode_num.toString().padStart(2, '0')}
                              </Text>
                              <Text size="sm" style={{ flex: 1 }}>
                                {episode.title || 'Untitled Episode'}
                              </Text>
                              <ActionIcon
                                variant="subtle"
                                color="gray"
                                onClick={() => setPreviewEpisode(episode)}
                                aria-label={`Preview S${episode.season.toString().padStart(2, '0')}E${episode.episode_num.toString().padStart(2, '0')}`}
                              >
                                <IconEye size={16} />
                              </ActionIcon>
                            </Group>
                          ))}
                        </Stack>
                      </Accordion.Panel>
                    </Accordion.Item>
                  )
                })}
              </Accordion>
            </ScrollArea>
          )}

          <Group justify="space-between" mt="md">
            <Text size="sm" c="dimmed">
              {selectedCount} episode{selectedCount === 1 ? '' : 's'} selected
            </Text>
            <Group>
              <Button variant="subtle" onClick={onClose}>
                Close
              </Button>
              <Button
                leftSection={<IconListCheck size={16} />}
                onClick={handleDownload}
                loading={downloadMutation.isPending}
                disabled={selectedIds.size === 0}
              >
                Download Selected
              </Button>
            </Group>
          </Group>
        </Stack>
      </Modal>

      {/* A sibling, not a child: a modal nested inside another modal inherits
          its stacking context and renders behind it. */}
      <VodPreviewModal
        opened={previewEpisode != null}
        onClose={() => setPreviewEpisode(null)}
        accountId={accountId}
        kind="episode"
        itemId={previewEpisode?.id}
        seriesId={series.series_id?.toString()}
        containerExtension={previewEpisode?.container_extension}
        title={previewEpisode?.title || 'Untitled Episode'}
        subtitle={`${series.name} — S${(previewEpisode?.season ?? 0).toString().padStart(2, '0')}E${(previewEpisode?.episode_num ?? 0).toString().padStart(2, '0')}`}
      />
    </>
  )
}
