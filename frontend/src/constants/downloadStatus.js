// Statuses a download can no longer move on from. The history list, the
// history filter and the WebSocket handlers on two pages all have to agree on
// this list, so it lives in one place.
export const TERMINAL_DOWNLOAD_STATUSES = ['completed', 'failed', 'cancelled', 'interrupted']

// Statuses whose recording is a real file sitting in the completed folder, so
// it can be played, downloaded, and shown with its filename and size. An
// interrupted recording is short but real.
export const PLAYABLE_DOWNLOAD_STATUSES = ['completed', 'interrupted']

// Statuses a user can start over from.
export const RETRYABLE_DOWNLOAD_STATUSES = ['failed', 'cancelled', 'interrupted']
