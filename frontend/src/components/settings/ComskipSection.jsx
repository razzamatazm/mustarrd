import {
  Alert,
  Anchor,
  Button,
  Checkbox,
  Group,
  Select,
  Stack,
  Text,
  TextInput,
} from '@mantine/core'
import { IconInfoCircle } from '@tabler/icons-react'

import NumberStepper from './NumberStepper'
import { LabelWithTooltip, SectionHeader, SettingRow, StepperField, SubGroup } from './SettingsPrimitives'
import classes from './ComskipSection.module.css'

export const COMSKIP_DEFAULTS = {
  comskip_detect_method: 107,
  comskip_max_commercialbreak: 600,
  comskip_min_commercialbreak: 25,
  comskip_max_commercial_size: 125,
  comskip_min_commercial_size: 4,
  comskip_always_keep_first_seconds: 0,
  comskip_always_keep_last_seconds: 60,
  comskip_remove_before: 0,
  comskip_remove_after: 0,
  comskip_connect_blocks_with_logo: true,
  comskip_dynamic_ticker_tape: false,
  comskip_thread_count: 1,
  comskip_hw_decode_mode: 'none',
  comskip_use_custom_ini: false,
  comskip_custom_ini_path: null,
}

const DETECT_METHOD_OPTIONS = [
  { bit: 1, label: 'Black frames', description: 'Looks for dark frames commonly inserted between a show and an ad break.' },
  { bit: 2, label: 'Logo detection', description: 'Uses the appearance and disappearance of the channel logo as a boundary signal.' },
  { bit: 4, label: 'Scene change', description: 'Looks for rapid visual cuts; useful for some ads but may increase false matches.' },
  { bit: 8, label: 'Fuzzy logic', description: 'Uses Comskip\'s fuzzy scoring to combine weaker clues when classifying show and commercial blocks.' },
  { bit: 32, label: 'Aspect ratio change', description: 'Detects switches such as 16:9 programming to 4:3 commercial material.' },
  { bit: 64, label: 'Silence detection', description: 'Looks for brief silence around commercial boundaries.' },
]
const KNOWN_DETECT_BITS = DETECT_METHOD_OPTIONS.reduce((sum, option) => sum + option.bit, 0)

export function getComskipErrors(formData) {
  if (!formData) return {}
  const errors = {}
  const value = (field) => formData[field] ?? COMSKIP_DEFAULTS[field]
  if (value('comskip_min_commercialbreak') > value('comskip_max_commercialbreak')) {
    errors.commercialbreak = 'Min commercial break must be less than or equal to max commercial break.'
  }
  if (value('comskip_min_commercial_size') > value('comskip_max_commercial_size')) {
    errors.commercial_size = 'Min single commercial must be less than or equal to max single commercial.'
  }
  if (formData.comskip_use_custom_ini && !(formData.comskip_custom_ini_path || '').trim()) {
    errors.custom_ini = 'Enter the full path to a readable comskip.ini file.'
  }
  return errors
}

function RecordingTimeline() {
  return (
    <div>
      <div className={classes.timeline} aria-hidden="true">
        <div className={classes.segKeep} style={{ width: '4%' }} />
        <div className={classes.segShow} />
        <div className={classes.segAd} style={{ width: '12%' }} />
        <div className={classes.segShow} />
        <div className={classes.segAd} style={{ width: '9%' }} />
        <div className={classes.segShow} />
        <div className={classes.segKeep} style={{ width: '7%' }} />
      </div>
      <div className={classes.legend}>
        <span className={classes.legendKey}>
          <span className={classes.swatch} style={{ background: 'rgba(64,192,87,0.45)' }} />
          Kept show content
        </span>
        <span className={classes.legendKey}>
          <span className={classes.swatch} style={{ background: 'rgba(250,82,82,0.5)' }} />
          Detected commercials (cut)
        </span>
        <span className={classes.legendKey}>
          <span className={classes.swatch} style={{ background: 'rgba(245,159,0,0.55)' }} />
          Protected (never cut)
        </span>
      </div>
    </div>
  )
}

const HW_DECODE_FALLBACK_MODES = [
  { id: 'none', name: 'None (software)', available: true },
  { id: 'hwassist', name: 'Hardware assist', available: false },
  { id: 'nvidia', name: 'NVIDIA (CUVID)', available: false },
]

export default function ComskipSection({ formData, onChange, onResetDefaults, onNavigateToProcessing, hwDecodeModes }) {
  const enabled = Boolean(formData?.comskip_enabled)
  const useCustomIni = Boolean(formData?.comskip_use_custom_ini)
  const managedDisabled = !enabled || useCustomIni
  const errors = getComskipErrors(formData)

  const detectMethod = formData?.comskip_detect_method ?? COMSKIP_DEFAULTS.comskip_detect_method
  const checkedBits = DETECT_METHOD_OPTIONS
    .filter((option) => (detectMethod & option.bit) === option.bit)
    .map((option) => String(option.bit))

  const handleDetectChange = (values) => {
    const known = values.reduce((sum, v) => sum + Number(v), 0)
    // Preserve bits without a checkbox (e.g. closed captions = 16) so an
    // externally-set bitmask is not silently truncated by a save.
    const unknown = detectMethod & ~KNOWN_DETECT_BITS
    onChange('comskip_detect_method', known + unknown)
  }

  const stepperField = (field, label, tooltip, props = {}) => (
    <StepperField
      label={label}
      tooltip={tooltip}
      disabled={managedDisabled}
      value={formData?.[field] ?? COMSKIP_DEFAULTS[field]}
      onChange={(val) => onChange(field, typeof val === 'number' ? val : COMSKIP_DEFAULTS[field])}
      {...props}
    />
  )

  const managedDimClass = managedDisabled ? classes.dimmed : undefined

  return (
    <Stack gap="xl">
      <SectionHeader title="Commercial Skip" description="Tune how Comskip detects and removes ad breaks" />

      {!enabled && (
        <Alert color="blue" variant="light" icon={<IconInfoCircle size={16} />}>
          <Text size="sm">
            Comskip is not enabled. Turn it on in{' '}
            <Anchor onClick={onNavigateToProcessing}>Post-Processing</Anchor>{' '}
            to configure these settings.
          </Text>
        </Alert>
      )}

      <SubGroup label="Configuration source">
        <SettingRow
          label="Use a custom Comskip INI"
          description="When enabled, Comskip reads the file below exactly as supplied. Mustarrd does not merge its detection settings or dynamic ticker value into that file."
          tooltip="Use this only when you maintain a complete comskip.ini yourself. Turn it off to return to the settings managed on this page."
        >
          <Checkbox
            aria-label="Use a custom Comskip INI"
            disabled={!enabled}
            checked={useCustomIni}
            onChange={(e) => onChange('comskip_use_custom_ini', e.currentTarget.checked)}
          />
        </SettingRow>
        {useCustomIni && (
          <TextInput
            label={
              <LabelWithTooltip
                label="Custom Comskip INI path"
                tooltip="Enter the full path as seen by Mustarrd. In Docker, the file must be mounted inside the container."
              />
            }
            description="Docker users: enter the path inside the Mustarrd container. The default config directory is /app/config, so a typical path is /app/config/custom-comskip.ini. The file must be complete and readable; Mustarrd validates it when you save."
            placeholder="/path/to/comskip.ini"
            disabled={!enabled}
            error={errors.custom_ini}
            value={formData?.comskip_custom_ini_path || ''}
            onChange={(e) => onChange('comskip_custom_ini_path', e.target.value || null)}
            classNames={{ input: classes.monoInput }}
          />
        )}
        {useCustomIni && (
          <Alert color="yellow" variant="light" icon={<IconInfoCircle size={16} />}>
            <Text size="sm">
              Custom INI mode is active. The detection, timing, protection, ticker, logo, and thread settings below
              are saved but will not be sent to Comskip until custom mode is turned off.
            </Text>
          </Alert>
        )}
      </SubGroup>

      <Stack gap="xl" className={managedDimClass}>
        <SubGroup label="What a recording looks like">
          <RecordingTimeline />
        </SubGroup>

        <SubGroup label="Detection signals">
          <Text size="xs" c="dimmed" maw="60ch">
            Which signals Comskip looks for when finding commercial boundaries. The defaults work for most
            providers. Each enabled signal contributes evidence; enabling every signal can make detection slower
            or less reliable when a provider uses unusual transitions.
          </Text>
          <Checkbox.Group value={checkedBits} onChange={handleDetectChange}>
            <div className={classes.checkGrid}>
              {DETECT_METHOD_OPTIONS.map((option) => (
                <Checkbox
                  key={option.bit}
                  value={String(option.bit)}
                  aria-label={option.label}
                  label={(
                    <Stack gap={1}>
                      <Text size="sm">{option.label}</Text>
                      <Text size="xs" c="dimmed">{option.description}</Text>
                    </Stack>
                  )}
                  disabled={managedDisabled}
                />
              ))}
            </div>
          </Checkbox.Group>
        </SubGroup>

        <SubGroup label="Break timing">
          <Text size="xs" c="dimmed" maw="60ch">
            These limits decide which detected spans are plausible commercials. Values that are too narrow can
            miss real breaks; values that are too broad can classify parts of the show as ads.
          </Text>
          <div className={classes.grid2}>
            {stepperField(
              'comskip_min_commercialbreak',
              'Min commercial break',
              'Shortest stretch Comskip will call a commercial break. Lower values may cause false positives on short scene transitions. Recommended: 25.',
              { unit: 'sec', max: 3600, error: errors.commercialbreak },
            )}
            {stepperField(
              'comskip_max_commercialbreak',
              'Max commercial break',
              'Longest stretch of continuous commercials Comskip will mark as a single break. Increase if your provider runs long ad blocks. Recommended: 600.',
              { unit: 'sec', max: 3600 },
            )}
            {stepperField(
              'comskip_min_commercial_size',
              'Min single commercial',
              'Shortest a single commercial can be. Raise this to avoid false cuts on brief logo bumpers. Recommended: 4.',
              { unit: 'sec', max: 600, error: errors.commercial_size },
            )}
            {stepperField(
              'comskip_max_commercial_size',
              'Max single commercial',
              'Longest a single commercial can be. Spots longer than this are treated as show content. Recommended: 125.',
              { unit: 'sec', max: 600 },
            )}
          </div>
        </SubGroup>

        <SubGroup label="Show protection">
          <Text size="xs" c="dimmed" maw="60ch">
            Protection keeps known show content safe and controls how aggressively cut mode trims around a detected
            break. Start conservatively—trim values remove additional video outside Comskip&apos;s detected boundary.
          </Text>
          <div className={classes.grid2}>
            {stepperField(
              'comskip_always_keep_first_seconds',
              'Always keep first',
              'Never mark this many seconds at the start of the recording as commercial, regardless of what Comskip detects. Useful for providers that play a logo intro before the show.',
              { unit: 'sec', max: 3600 },
            )}
            {stepperField(
              'comskip_always_keep_last_seconds',
              'Always keep last',
              'Never mark this many seconds at the end of the recording as commercial. Prevents accidental cutting of end credits or a post-credits scene.',
              { unit: 'sec', max: 3600 },
            )}
            {stepperField(
              'comskip_remove_before',
              'Trim before each break',
              'Extra seconds of show content to cut immediately before each detected commercial block. Use with caution: removes show content.',
              { unit: 'sec', max: 120 },
            )}
            {stepperField(
              'comskip_remove_after',
              'Trim after each break',
              'Extra seconds of show content to cut immediately after each detected commercial block.',
              { unit: 'sec', max: 120 },
            )}
          </div>
        </SubGroup>

        <SubGroup label="Advanced">
          <Text size="xs" c="dimmed" maw="60ch">
            These settings affect the portion of the picture Comskip analyzes, how nearby detections are joined,
            and how much CPU the scan may use.
          </Text>
          <Stack gap={0}>
            <SettingRow
              label="Dynamic ticker exclusion"
              description="Before each scan, Mustarrd reads the recording resolution and tells Comskip to ignore the bottom one-ninth of the picture—80 px at 720p or 120 px at 1080p. Enable this when a persistent lower-third or ticker remains visible during ads and can make channel graphics look continuously present."
              tooltip="This is calculated independently for every recording. Excluding the ticker area can keep persistent bottom graphics from influencing logo and block classification, but may hide useful commercial evidence near the bottom of the frame."
            >
              <Checkbox
                aria-label="Dynamic ticker exclusion"
                disabled={managedDisabled}
                checked={Boolean(formData?.comskip_dynamic_ticker_tape)}
                onChange={(e) => onChange('comskip_dynamic_ticker_tape', e.currentTarget.checked)}
              />
            </SettingRow>
            <SettingRow
              label="Connect blocks with logo"
              description="Join neighboring detected blocks when the channel logo is visible at their transition. Enabled by default to match the bundled Comskip behavior; turn it off if logo-heavy channels merge show content into a break."
              tooltip="Corresponds to connect_blocks_with_logo in comskip.ini. Enable only if your provider splits a single ad break into several short blocks."
            >
              <Checkbox
                aria-label="Connect blocks with logo"
                disabled={managedDisabled}
                checked={Boolean(
                  formData?.comskip_connect_blocks_with_logo
                    ?? COMSKIP_DEFAULTS.comskip_connect_blocks_with_logo
                )}
                onChange={(e) => onChange(
                  'comskip_connect_blocks_with_logo',
                  e.currentTarget.checked,
                )}
              />
            </SettingRow>
            <SettingRow
              label="Processing threads"
              description="Number of CPU threads Comskip may use while decoding. The default of 1 gives the most repeatable detection behavior and minimizes contention with other work."
              tooltip="Some Comskip builds can produce different detection results with multiple decode threads. Increase only after validating the EDL for your channels. Higher values can also compete with downloads and ffmpeg jobs. Maximum: 16."
            >
              <NumberStepper
                aria-label="Processing threads"
                min={1}
                max={16}
                disabled={managedDisabled}
                value={formData?.comskip_thread_count ?? COMSKIP_DEFAULTS.comskip_thread_count}
                onChange={(val) => onChange('comskip_thread_count', typeof val === 'number' ? val : COMSKIP_DEFAULTS.comskip_thread_count)}
              />
            </SettingRow>
            <SettingRow
              label="Hardware decoding"
              description="Let the GPU decode video during commercial detection, which can cut detection time noticeably on long recordings."
              tooltip="Comskip cannot report which decoders it supports, so unavailable options stay listed. If the selected mode does not work, detection automatically re-runs on the CPU and the recording still completes."
            >
              {/* A command-line flag, not an INI key, so a custom INI does not
                  take this over the way it does the tunables above. */}
              <Select
                aria-label="Hardware decoding"
                disabled={!enabled}
                allowDeselect={false}
                value={formData?.comskip_hw_decode_mode ?? COMSKIP_DEFAULTS.comskip_hw_decode_mode}
                onChange={(val) => onChange('comskip_hw_decode_mode', val || COMSKIP_DEFAULTS.comskip_hw_decode_mode)}
                data={(hwDecodeModes?.length ? hwDecodeModes : HW_DECODE_FALLBACK_MODES).map((mode) => ({
                  value: mode.id,
                  label: mode.available ? mode.name : `${mode.name} (not detected)`,
                }))}
              />
            </SettingRow>
          </Stack>
          {(formData?.comskip_thread_count ?? COMSKIP_DEFAULTS.comskip_thread_count) > 1 && (
            <Alert color="orange" variant="light">
              <Text size="sm" fw={500}>Higher thread counts can change detection results</Text>
              <Text size="xs" mt={3}>
                Comskip may not produce identical commercial boundaries when multiple decode threads are used. Validate
                the EDL after increasing this value and return to 1 if breaks are missed or detection becomes less
                accurate. Higher values can also increase CPU contention with downloads and ffmpeg jobs.
              </Text>
            </Alert>
          )}
        </SubGroup>
      </Stack>

      <Group>
        <Button variant="default" size="xs" disabled={!enabled} onClick={onResetDefaults}>
          Reset to Defaults
        </Button>
      </Group>
    </Stack>
  )
}
