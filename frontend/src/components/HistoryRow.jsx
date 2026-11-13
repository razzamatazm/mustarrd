import { useState } from 'react'
import { IconAlertTriangle, IconCheck, IconX } from '@tabler/icons-react'
import classes from './HistoryRow.module.css'

// Compact history row shared by Downloads History and Scheduled History.
// `actions` is a node of icon buttons (ActionIcon) built by the caller.
//
// `poster` is the recording's poster art. Pass a URL to show it, `null` to
// keep its slot empty so titles line up with rows that have one, or leave it
// out entirely for a list that never has posters.
export default function HistoryRow({ status, title, subtitle, error, size, actions, poster }) {
  const [failedPoster, setFailedPoster] = useState(null)
  const Icon =
    status === 'completed' ? IconCheck : status === 'interrupted' ? IconAlertTriangle : IconX
  const iconClass = classes[status] || classes.cancelled
  const showPoster = Boolean(poster) && failedPoster !== poster

  return (
    <div className={classes.row}>
      <span className={`${classes.icon} ${iconClass}`}>
        <Icon size={15} />
      </span>
      {poster !== undefined && (
        <span className={classes.poster} data-testid="history-poster-slot">
          {showPoster && (
            <img
              src={poster}
              alt=""
              loading="lazy"
              onError={() => setFailedPoster(poster)}
            />
          )}
        </span>
      )}
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
