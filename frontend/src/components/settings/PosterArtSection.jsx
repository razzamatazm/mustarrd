import { useState } from 'react'
import { Anchor, Button, Group, PasswordInput, Stack, Switch, Text } from '@mantine/core'

import { settingsApi } from '../../api'
import { SectionHeader, SettingRow, SubGroup } from './SettingsPrimitives'

const CHECK_COLORS = { true: 'green', false: 'red', null: 'yellow' }

// The TMDB key is write-only: the server says whether one is saved
// (`tmdb_api_key_set`) but never sends it back. In the form, `tmdb_api_key`
// is a string to save a new key, '' to remove the saved one, and null or
// missing for "leave it alone" - so typing a key and then deleting it again
// goes back to null rather than silently removing what was saved.
export default function PosterArtSection({ formData, onChange, switchSize }) {
  const [checkResult, setCheckResult] = useState(null)
  const [checking, setChecking] = useState(false)

  const enabled = formData?.poster_art_enabled !== false
  const typedKey = typeof formData?.tmdb_api_key === 'string' ? formData.tmdb_api_key : null
  const removing = typedKey === ''
  const hasSavedKey = Boolean(formData?.tmdb_api_key_set)
  const canCheck = Boolean(typedKey) || (hasSavedKey && !removing)

  const handleKeyChange = (value) => {
    setCheckResult(null)
    onChange('tmdb_api_key', value.trim() ? value : null)
  }

  const handleCheck = async () => {
    setChecking(true)
    setCheckResult(null)
    try {
      setCheckResult(await settingsApi.checkPosterArtKey(typedKey || undefined))
    } catch (error) {
      setCheckResult({ ok: null, message: error.message || 'The key could not be checked.' })
    } finally {
      setChecking(false)
    }
  }

  let keyDescription = 'Optional. Paste the API key or the longer API Read Access Token from your TMDB account.'
  if (removing) {
    keyDescription = 'The saved key will be removed when you save.'
  } else if (hasSavedKey && !typedKey) {
    keyDescription = 'A key is saved. Paste a new one here to replace it.'
  }

  return (
    <Stack gap="xl">
      <SectionHeader
        title="Poster Art"
        description="Posters for finished recordings, in History and in your library"
      />

      <Text size="sm" c="dimmed" maw="70ch">
        When a recording finishes, Mustarrd looks for its poster. To do that it sends the name of the
        show, and its ID when your guide has one, to TVMaze, and to TMDB if you add a key below.
        Nothing about you or your provider is sent. A recording with no poster is left as it is, and
        looking for one can never delay or fail a recording.
      </Text>

      <Stack gap={0}>
        <SettingRow
          label="Fetch poster art"
          description="Look up a poster for each finished recording and show it in History"
        >
          <Switch
            size={switchSize}
            aria-label="Fetch poster art"
            checked={enabled}
            onChange={(e) => onChange('poster_art_enabled', e.currentTarget.checked)}
          />
        </SettingRow>
        <SettingRow
          label="Save a copy next to the recording"
          description="Puts the poster in your library. A show in its own folder gets poster.jpg in that folder, which is where Kodi and Jellyfin look for a show's poster. Anything else gets a file named after the recording, ending in -poster.jpg, which they use for films but not for single episodes."
        >
          <Switch
            size={switchSize}
            aria-label="Save a copy next to the recording"
            disabled={!enabled}
            checked={enabled && formData?.poster_art_save_to_library !== false}
            onChange={(e) => onChange('poster_art_save_to_library', e.currentTarget.checked)}
          />
        </SettingRow>
      </Stack>

      <SubGroup label="TMDB key">
        <Text size="sm" c="dimmed" maw="70ch">
          Without a key, posters for TV shows come from TVMaze and films get none. With a free TMDB
          key, films get posters too and TMDB is asked first. You can get one from{' '}
          <Anchor href="https://www.themoviedb.org/settings/api" target="_blank" rel="noreferrer" size="sm">
            your TMDB account settings
          </Anchor>
          .
        </Text>
        <PasswordInput
          aria-label="TMDB API key"
          label="TMDB API key"
          description={keyDescription}
          placeholder={hasSavedKey && !removing ? 'Saved' : 'Paste your key here'}
          autoComplete="off"
          disabled={!enabled}
          value={typedKey || ''}
          onChange={(e) => handleKeyChange(e.target.value)}
        />
        <Group gap="sm">
          <Button
            variant="default"
            size="xs"
            loading={checking}
            disabled={!enabled || !canCheck}
            onClick={handleCheck}
          >
            Check key
          </Button>
          {hasSavedKey && !removing && (
            <Button
              variant="subtle"
              color="red"
              size="xs"
              onClick={() => {
                setCheckResult(null)
                onChange('tmdb_api_key', '')
              }}
            >
              Remove saved key
            </Button>
          )}
          {removing && (
            <Button
              variant="subtle"
              size="xs"
              onClick={() => onChange('tmdb_api_key', null)}
            >
              Keep saved key
            </Button>
          )}
        </Group>
        {checkResult && (
          <Text size="sm" c={CHECK_COLORS[String(checkResult.ok)] || 'yellow'} role="status">
            {checkResult.message}
          </Text>
        )}
      </SubGroup>
    </Stack>
  )
}
