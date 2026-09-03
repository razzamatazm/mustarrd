import {
  IconCalendar,
  IconDeviceTv,
  IconDownload,
  IconFile,
  IconListDetails,
  IconLock,
  IconMoon,
  IconScissors,
  IconServer,
  IconUsers,
  IconWand,
} from '@tabler/icons-react'

// The settings IA: 3 groups, order matters.
export const SECTION_GROUPS = [
  {
    label: 'Connections',
    items: [
      { id: 'accounts', label: 'Accounts', icon: IconServer },
      { id: 'plex', label: 'Plex Integration', icon: IconDeviceTv },
      { id: 'users', label: 'Users', icon: IconUsers },
    ],
  },
  {
    label: 'Recording',
    items: [
      { id: 'recording', label: 'Recording', icon: IconDownload },
      { id: 'processing', label: 'Post-Processing', icon: IconWand },
      { id: 'comskip', label: 'Commercial Skip', icon: IconScissors },
      { id: 'naming', label: 'File Naming', icon: IconFile },
    ],
  },
  {
    label: 'System',
    items: [
      { id: 'guide', label: 'Program Guide', icon: IconCalendar },
      { id: 'appearance', label: 'Appearance', icon: IconMoon },
      { id: 'security', label: 'Security', icon: IconLock },
      { id: 'logs', label: 'Logs', icon: IconListDetails },
    ],
  },
]

export const ADMIN_SECTIONS = SECTION_GROUPS.flatMap((group) => group.items)

export const DOWNLOAD_USER_SECTIONS = [
  { id: 'security', label: 'Security', icon: IconLock },
]

// id -> { label, group } for search breadcrumbs and screen labels.
export const SECTION_LABELS = SECTION_GROUPS.reduce((acc, group) => {
  group.items.forEach((item) => {
    acc[item.id] = { label: item.label, group: group.label }
  })
  return acc
}, {})

// Client-side search index: every setting maps to its section.
export const SEARCH_INDEX = [
  { label: 'Download folder', section: 'recording', kw: 'storage path temp' },
  { label: 'Completed folder', section: 'recording', kw: 'storage path media library output' },
  { label: 'Max concurrent downloads', section: 'recording', kw: 'simultaneous parallel limit' },
  { label: 'Minimum free space', section: 'recording', kw: 'disk gb pause low' },
  { label: 'Scheduled download delay', section: 'recording', kw: 'wait after airing catchup provider archive minutes' },
  { label: 'Recording padding (start early / end late)', section: 'recording', kw: 'minutes before after buffer' },
  { label: 'Output container (.ts / MKV / MP4)', section: 'processing', kw: 'format remux transcode encode ffmpeg' },
  { label: 'Commercials', section: 'processing', kw: 'comskip ads skip mark cut commercials' },
  { label: 'Hardware acceleration', section: 'processing', kw: 'gpu vaapi intel quick sync encode' },
  { label: 'Delete original after processing', section: 'processing', kw: 'source ts cleanup' },
  { label: 'Max concurrent post-processing', section: 'processing', kw: 'parallel conversion limit' },
  { label: 'Commercial detection signals', section: 'comskip', kw: 'black frames logo silence scene' },
  { label: 'Commercial break timing', section: 'comskip', kw: 'min max seconds ad block' },
  { label: 'Show protection (keep first/last)', section: 'comskip', kw: 'credits intro never cut' },
  { label: 'Comskip threads', section: 'comskip', kw: 'cpu processing performance' },
  { label: 'Custom Comskip INI', section: 'comskip', kw: 'override config file' },
  { label: 'TV show filename template', section: 'naming', kw: 'season episode SxxExx plex naming' },
  { label: 'Movie filename template', section: 'naming', kw: 'year title naming' },
  { label: 'Sports filename template', section: 'naming', kw: 'league teams date naming' },
  { label: 'IPTV provider accounts', section: 'accounts', kw: 'xtream server url username add' },
  { label: 'Force EPG refresh', section: 'accounts', kw: 'rebuild guide clear' },
  { label: 'Plex server & libraries', section: 'plex', kw: 'connect library scan refresh' },
  { label: 'Allow Plex users to sign in', section: 'plex', kw: 'login request access' },
  { label: 'Download-only users', section: 'users', kw: 'add member password local' },
  { label: 'Guide refresh interval', section: 'guide', kw: 'epg sync hours automatic' },
  { label: 'Theme (dark / light)', section: 'appearance', kw: 'mode color' },
  { label: 'Admin password', section: 'security', kw: 'change credentials login' },
  { label: 'Application logs', section: 'logs', kw: 'errors warnings activity debug' },
]
