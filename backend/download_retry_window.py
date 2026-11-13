"""How long a download keeps trying when the provider drops out mid-recording.

A connection reset or read timeout partway through a recording is retried, and
each retry resumes from the bytes already on disk. The user-facing knob is one
number: the **retry window**, the total time a recording will spend waiting
for the provider to come back before it gives up.

The window is spent in growing waits - 5s, 10s, 15s and so on, capped at 30s
so a long window buys more attempts rather than longer gaps. The final wait is
trimmed so the waits add up to exactly the window.

The default is computed from the constants that used to be hard-coded (three
retries with a 5s linear backoff), so an install that never touches the
setting behaves exactly as it did: retries after 5, 10 and 15 seconds, then
give up.

The window counts waiting only. An attempt that hangs before failing (a
provider that accepts the connection and then goes silent) takes its own
timeout on top, so wall-clock time can run longer than the window.
"""

from typing import List

# The pre-setting behaviour: retries after the initial attempt, and the base
# delay that was multiplied by the attempt number.
TRANSIENT_RETRY_LIMIT = 3
TRANSIENT_RETRY_BACKOFF_SECONDS = 5.0

# Past this a single wait stops growing.
MAX_RETRY_DELAY_SECONDS = 30.0

DEFAULT_RETRY_WINDOW_SECONDS = int(
    sum(TRANSIENT_RETRY_BACKOFF_SECONDS * attempt for attempt in range(1, TRANSIENT_RETRY_LIMIT + 1))
)
MAX_RETRY_WINDOW_SECONDS = 3600


def resolve_retry_window_seconds(value) -> int:
    """A stored window that is safe to act on; anything else is the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return DEFAULT_RETRY_WINDOW_SECONDS
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_WINDOW_SECONDS
    if not 0 <= seconds <= MAX_RETRY_WINDOW_SECONDS:
        return DEFAULT_RETRY_WINDOW_SECONDS
    return seconds


def retry_delays_for_window(window_seconds: int) -> List[float]:
    """The wait before each retry, in order. Its length is the retry count."""
    remaining = float(resolve_retry_window_seconds(window_seconds))
    delays: List[float] = []
    while remaining > 0:
        attempt = len(delays) + 1
        delay = min(
            TRANSIENT_RETRY_BACKOFF_SECONDS * attempt, MAX_RETRY_DELAY_SECONDS, remaining
        )
        delays.append(delay)
        remaining -= delay
    return delays
