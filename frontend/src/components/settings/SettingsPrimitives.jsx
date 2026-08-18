import { Alert, Box, Code, Divider, Group, Stack, Text, Tooltip } from '@mantine/core'
import { IconAlertCircle, IconInfoCircle } from '@tabler/icons-react'

import NumberStepper from './NumberStepper'
import classes from './SettingsPrimitives.module.css'

export function SectionHeader({ title, description }) {
  return (
    <Stack gap={2}>
      <Text fw={700} size="lg">{title}</Text>
      {description && <Text size="sm" c="dimmed">{description}</Text>}
      {title === 'File Naming' && (
        <Alert color="blue" variant="light" icon={<IconAlertCircle size={16} />} mt="sm">
          A forward slash in a template creates a folder. For example{' '}
          <Code>{'TV Shows/{show}/Season {season:02d}/{show} - S{season:02d}E{episode:02d}'}</Code> files the
          recording into nested folders inside your completed folder. Slashes that come from the guide itself
          (a show called &quot;AC/DC&quot;, say) stay part of the name and don&apos;t create folders.
        </Alert>
      )}
    </Stack>
  )
}

// Uppercase subhead followed by a dashed rule filling the remaining width.
export function SubGroup({ label, children, ...rest }) {
  return (
    <Stack gap="sm" {...rest}>
      <Divider
        variant="dashed"
        labelPosition="left"
        label={
          <Text size="11px" fw={700} tt="uppercase" c="dimmed" style={{ letterSpacing: '0.08em' }}>
            {label}
          </Text>
        }
      />
      {children}
    </Stack>
  )
}

export function LabelWithTooltip({ label, tooltip }) {
  if (!tooltip) {
    return <span>{label}</span>
  }
  return (
    <Group component="span" gap={4} wrap="nowrap" display="inline-flex" align="center">
      <span>{label}</span>
      <Tooltip label={tooltip} multiline w={250} withArrow events={{ hover: true, focus: true, touch: true }}>
        <IconInfoCircle size={13} style={{ opacity: 0.6, flexShrink: 0 }} aria-label={`About ${label}`} />
      </Tooltip>
    </Group>
  )
}

// Label + control on the same line; description (and error) below the label line.
// Consecutive rows get a 1px separator via the module CSS.
export function SettingRow({ label, description, tooltip, error, children }) {
  return (
    <Box className={classes.row}>
      <Box className={classes.rowMain}>
        <Text fw={500} size="sm" style={{ minWidth: 0 }}>
          <LabelWithTooltip label={label} tooltip={tooltip} />
        </Text>
        <Box className={classes.rowControl}>{children}</Box>
      </Box>
      {description && (
        <Text size="xs" c="dimmed" maw="60ch" mt={2}>{description}</Text>
      )}
      {error && (
        <Text size="xs" c="red" mt={2}>{error}</Text>
      )}
    </Box>
  )
}

// Stacked label-above-stepper field for grid layouts (Commercial Skip).
export function StepperField({ label, tooltip, error, ...stepperProps }) {
  return (
    <Stack gap={4}>
      <Text fw={500} size="sm">
        <LabelWithTooltip label={label} tooltip={tooltip} />
      </Text>
      <NumberStepper aria-label={label} error={Boolean(error)} {...stepperProps} />
      {error && <Text size="xs" c="red">{error}</Text>}
    </Stack>
  )
}
