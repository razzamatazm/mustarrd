import { IconAlertTriangle, IconCheck, IconX } from '@tabler/icons-react'
import classes from './HistoryRow.module.css'

// Compact history row shared by Downloads History and Scheduled History.
// `actions` is a node of icon buttons (ActionIcon) built by the caller.
export default function HistoryRow({ status, title, subtitle, error, size, actions }) {
  const Icon =
    status === 'completed' ? IconCheck : status === 'interrupted' ? IconAlertTriangle : IconX
  const iconClass = classes[status] || classes.cancelled

  return (
    <div className={classes.row}>
      <span className={`${classes.icon} ${iconClass}`}>
        <Icon size={15} />
      </span>
      <div className={classes.main}>
        <div className={classes.title}>{title}</div>
        {subtitle && <div className={classes.sub}>{subtitle}</div>}
        {error && <div className={classes.err}>{error}</div>}
      </div>
      {size && <span className={classes.size}>{size}</span>}
      {actions && <div className={classes.actions}>{actions}</div>}
    </div>
  )
}
