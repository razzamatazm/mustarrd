import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Group,
  NumberInput,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { IconInfoCircle } from '@tabler/icons-react'

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
  comskip_thread_count: 1,
}

const DETECT_METHOD_OPTIONS = [
  { bit: 1, label: 'Black frames' },
  { bit: 2, label: 'Logo detection' },
  { bit: 4, label: 'Scene change' },
  { bit: 8, label: 'Resolution change' },
  { bit: 32, label: 'Aspect ratio change' },
  { bit: 64, label: 'Silence detection' },
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
  return errors
}

function LabelWithTooltip({ label, tooltip }) {
  return (
    <Group component="span" gap={4} wrap="nowrap" display="inline-flex">
      <span>{label}</span>
      <Tooltip label={tooltip} multiline w={300} withArrow events={{ hover: true, focus: true, touch: true }}>
        <IconInfoCircle size={14} style={{ opacity: 0.6, flexShrink: 0 }} aria-label={`About ${label}`} />
      </Tooltip>
    </Group>
  )
}

export default function ComskipSection({ formData, onChange, onResetDefaults }) {
  const enabled = Boolean(formData?.comskip_enabled)
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

  const numberField = (field, label, tooltip, props = {}) => (
    <NumberInput
      label={<LabelWithTooltip label={label} tooltip={tooltip} />}
      min={0}
      disabled={!enabled}
      allowDecimal={false}
      value={formData?.[field] ?? COMSKIP_DEFAULTS[field]}
      onChange={(val) => onChange(field, typeof val === 'number' ? val : COMSKIP_DEFAULTS[field])}
      {...props}
    />
  )

  return (
    <Stack gap="lg">
      <Stack gap={2}>
        <Text fw={600} size="lg">Comskip</Text>
        <Text size="sm" c="dimmed">Tune how Comskip detects and removes commercials</Text>
      </Stack>

      {!enabled && (
        <Alert color="blue" variant="light" icon={<IconInfoCircle size={16} />}>
          <Text size="sm">
            Comskip is not enabled. Turn it on in Post-Processing to configure these settings.
          </Text>
        </Alert>
      )}

      <Stack gap="md">
        <Text size="xs" fw={600} c="dimmed" tt="uppercase" style={{ letterSpacing: '0.06em' }}>Commercial Detection</Text>
        <Checkbox.Group
          label={
            <LabelWithTooltip
              label="Detection methods"
              tooltip="Which signals Comskip looks for when finding commercial boundaries. The default enables black frames, logo presence, resolution change, aspect ratio changes, and silence detection. Adding more signals rarely helps and slows processing."
            />
          }
          value={checkedBits}
          onChange={handleDetectChange}
        >
          <Stack gap="xs" mt="xs">
            {DETECT_METHOD_OPTIONS.map((option) => (
              <Checkbox
                key={option.bit}
                value={String(option.bit)}
                label={option.label}
                disabled={!enabled}
              />
            ))}
          </Stack>
        </Checkbox.Group>
      </Stack>

      <Divider variant="dashed" />

      <Stack gap="md">
        <Text size="xs" fw={600} c="dimmed" tt="uppercase" style={{ letterSpacing: '0.06em' }}>Commercial Timing</Text>
        <Group grow align="flex-start">
          {numberField(
            'comskip_min_commercialbreak',
            'Min commercial break (seconds)',
            'Shortest stretch Comskip will call a commercial break. Lower values may cause false positives on short scene transitions. Recommended: 25.',
            { error: errors.commercialbreak },
          )}
          {numberField(
            'comskip_max_commercialbreak',
            'Max commercial break (seconds)',
            'Longest stretch of continuous commercials Comskip will mark as a single break. Increase if your provider runs long ad blocks. Recommended: 600.',
          )}
        </Group>
        <Group grow align="flex-start">
          {numberField(
            'comskip_min_commercial_size',
            'Min single commercial (seconds)',
            'Shortest a single commercial can be. Raise this to avoid false cuts on brief logo bumpers. Recommended: 4.',
            { error: errors.commercial_size },
          )}
          {numberField(
            'comskip_max_commercial_size',
            'Max single commercial (seconds)',
            'Longest a single commercial can be. Spots longer than this are treated as show content. Recommended: 125.',
          )}
        </Group>
      </Stack>

      <Divider variant="dashed" />

      <Stack gap="md">
        <Text size="xs" fw={600} c="dimmed" tt="uppercase" style={{ letterSpacing: '0.06em' }}>Show Protection</Text>
        <Group grow align="flex-start">
          {numberField(
            'comskip_always_keep_first_seconds',
            'Always keep first N seconds',
            'Never mark this many seconds at the start of the recording as commercial, regardless of what Comskip detects. Useful for providers that play a logo intro before the show.',
          )}
          {numberField(
            'comskip_always_keep_last_seconds',
            'Always keep last N seconds',
            'Never mark this many seconds at the end of the recording as commercial. Prevents accidental cutting of end credits or a post-credits scene.',
          )}
        </Group>
        <Group grow align="flex-start">
          {numberField(
            'comskip_remove_before',
            'Remove N seconds before each break',
            'Extra seconds of show content to cut immediately before each detected commercial block. Use with caution: removes show content.',
          )}
          {numberField(
            'comskip_remove_after',
            'Remove N seconds after each break',
            'Extra seconds of show content to cut immediately after each detected commercial block.',
          )}
        </Group>
        {numberField(
          'comskip_thread_count',
          'Processing threads',
          'Number of CPU threads Comskip uses. More threads = faster processing but more CPU load during recording. Maximum: 16.',
          { min: 1, max: 16 },
        )}
      </Stack>

      <Divider variant="dashed" />

      <Stack gap="md">
        <TextInput
          label={
            <LabelWithTooltip
              label="Custom Comskip INI path (optional)"
              tooltip="If set, this file overrides the generated settings above. Leave blank to use the settings on this page."
            />
          }
          placeholder="/path/to/comskip.ini"
          disabled={!enabled}
          value={formData?.comskip_custom_ini_path || ''}
          onChange={(e) => onChange('comskip_custom_ini_path', e.target.value || null)}
        />
        <Group>
          <Button variant="default" disabled={!enabled} onClick={onResetDefaults}>
            Reset to Defaults
          </Button>
        </Group>
      </Stack>
    </Stack>
  )
}
