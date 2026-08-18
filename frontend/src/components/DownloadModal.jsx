import { useState, useEffect } from 'react'
import {
  Modal,
  Stack,
  Text,
  TextInput,
  Button,
  Group,
  Badge,
  Loader,
  Alert,
  Accordion,
  NumberInput,
  useMantineColorScheme,
  useMantineTheme,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  IconDownload,
  IconClock,
  IconVideo,
  IconPlayerPlay,
  IconTrophy,
  IconFile,
} from '@tabler/icons-react'

import { downloadsApi, settingsApi } from '../api'
import { formatChannelDateTime, getChannelDisplayTime, getGuideOffsetHours } from '../utils/channelTime'

const typeIcons = {
  tv_show: IconVideo,
  movie: IconPlayerPlay,
  sports: IconTrophy,
  other: IconFile,
  news: IconFile,
}

const typeLabels = {
  tv_show: 'TV Show',
  movie: 'Movie',
  sports: 'Sports',
  news: 'News',
  other: 'Other',
}

export default function DownloadModal({ opened, onClose, program, channel, accountId, guideOffsetHours = 0 }) {
  const queryClient = useQueryClient()
  const [filename, setFilename] = useState('')
  const [detectedType, setDetectedType] = useState('other')
  const [isLoadingPreview, setIsLoadingPreview] = useState(false)
  const [prePadding, setPrePadding] = useState(1)
  const [postPadding, setPostPadding] = useState(5)
  const [offsetsDirty, setOffsetsDirty] = useState(false)
  const { colorScheme } = useMantineColorScheme()
  const theme = useMantineTheme()
  const normalizedGuideOffsetHours = getGuideOffsetHours(guideOffsetHours)

  const accordionStyles = {
    item: {
      borderRadius: theme.radius.md,
      border: `1px solid ${colorScheme === 'dark' ? theme.colors.dark[6] : theme.colors.gray[5]}`,
      backgroundColor: colorScheme === 'dark' ? theme.colors.dark[7] : theme.colors.gray[1],
    },
    control: {
      borderRadius: theme.radius.md,
    },
    panel: {
      borderTop: `1px solid ${colorScheme === 'dark' ? theme.colors.dark[6] : theme.colors.gray[3]}`,
    },
  }

  const { data: settings } = useQuery({
    queryKey: ['settings', 'public'],
    queryFn: settingsApi.getPublic,
  })

  useEffect(() => {
    if (opened) {
      setOffsetsDirty(false)
    }
  }, [opened])

  useEffect(() => {
    if (!opened || offsetsDirty) {
      return
    }
    const defaultPre = settings?.default_pre_padding_minutes ?? 1
    const defaultPost = settings?.default_post_padding_minutes ?? 5
    setPrePadding(defaultPre)
    setPostPadding(defaultPost)
  }, [opened, offsetsDirty, settings])

  useEffect(() => {
    if (opened && program && channel && accountId) {
      setIsLoadingPreview(true)
      downloadsApi
        .previewFilename({
          account_id: parseInt(accountId),
          channel_id: channel.stream_id.toString(),
          channel_name: channel.name,
          program: program,
        })
        .then((data) => {
          setFilename(data.filename.replace(/\.ts$/, ''))
          setDetectedType(data.detected_type)
        })
        .catch((err) => {
          console.error('Failed to get filename preview:', err)
          const date = formatChannelDateTime(program, 'start', normalizedGuideOffsetHours, 'YYYY-MM-DD')
          setFilename(`${channel.name} - ${program.title} - ${date}`)
        })
        .finally(() => {
          setIsLoadingPreview(false)
        })
    }
  }, [accountId, channel, normalizedGuideOffsetHours, opened, program])

  const downloadMutation = useMutation({
    mutationFn: (data) => downloadsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['downloads'] })
      notifications.show({
        title: 'Download Queued',
        message: 'Your download has been added to the queue',
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
    downloadMutation.mutate({
      account_id: parseInt(accountId),
      channel_id: channel.stream_id.toString(),
      channel_name: channel.name,
      program: program,
      custom_filename: filename ? `${filename}.ts` : null,
      pre_padding_minutes: prePadding,
      post_padding_minutes: postPadding,
    })
  }

  if (!program || !channel) {
    return null
  }

  const startTime = getChannelDisplayTime(program, 'start', normalizedGuideOffsetHours)
  const endTime = getChannelDisplayTime(program, 'end', normalizedGuideOffsetHours)
  const TypeIcon = typeIcons[detectedType] || IconFile
  const totalDuration = (program.duration_minutes || 0) + prePadding + postPadding
  const adjustedStart = startTime?.subtract(prePadding, 'minute')
  const adjustedEnd = endTime?.add(postPadding, 'minute')

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Download Program"
      size="lg"
      returnFocus={false}
    >
      <Stack>
        <Stack gap="xs">
          <Text fw={600} size="lg">
            {program.title}
          </Text>
          <Text size="sm" c="dimmed">
            {channel.name}
          </Text>
        </Stack>

        <Group gap="xs">
          <Badge variant="light" leftSection={<IconClock size={12} />}>
            {startTime?.format('MMM D, YYYY h:mm A') || 'Unknown'}
          </Badge>
          <Badge variant="light">
            {program.duration_minutes} minutes
          </Badge>
          <Badge variant="light" color="blue" leftSection={<TypeIcon size={12} />}>
            {typeLabels[detectedType]}
          </Badge>
        </Group>

        {program.description && (
          <Alert variant="light" color="gray">
            <Text size="sm">{program.description}</Text>
          </Alert>
        )}

        <Accordion variant="separated" styles={accordionStyles}>
          <Accordion.Item value="offsets">
            <Accordion.Control>Recording Offsets</Accordion.Control>
            <Accordion.Panel>
              <Stack gap="sm">
                <NumberInput
                  label="Minutes before start"
                  description="Start recording early"
                  min={0}
                  max={120}
                  value={prePadding}
                  onChange={(val) => {
                    setOffsetsDirty(true)
                    setPrePadding(typeof val === 'number' ? val : 0)
                  }}
                />
                <NumberInput
                  label="Minutes after end"
                  description="Keep recording after the program ends"
                  min={0}
                  max={120}
                  value={postPadding}
                  onChange={(val) => {
                    setOffsetsDirty(true)
                    setPostPadding(typeof val === 'number' ? val : 0)
                  }}
                />
                <Text size="sm" c="dimmed">
                  Recording starts at {adjustedStart?.format('MMM D, YYYY h:mm A') || 'Unknown'} and runs for{' '}
                  {totalDuration} minutes, ending at {adjustedEnd?.format('MMM D, YYYY h:mm A') || 'Unknown'}.
                </Text>
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>

        <Stack gap="xs">
          <Text fw={500}>Filename</Text>
          {isLoadingPreview ? (
            <Group>
              <Loader size="sm" />
              <Text size="sm" c="dimmed">Generating filename...</Text>
            </Group>
          ) : (
            <TextInput
              value={filename}
              onChange={(e) => setFilename(e.target.value)}
              rightSection={<Text size="xs" c="dimmed">.ts</Text>}
              description="Auto-generated based on program type. Edit if needed."
            />
          )}
        </Stack>

        <Group justify="flex-end" mt="md">
          <Button variant="subtle" onClick={onClose}>
            Cancel
          </Button>
          <Button
            leftSection={<IconDownload size={16} />}
            onClick={handleDownload}
            loading={downloadMutation.isPending}
            disabled={isLoadingPreview || !filename}
          >
            Download
          </Button>
        </Group>
      </Stack>
    </Modal>
  )
}
