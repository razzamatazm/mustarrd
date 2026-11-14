"""Shared limits for the existing failed-download recovery lifecycle."""
AUTO_RETRY_MAX_ATTEMPTS = 3
AUTO_RETRY_MIN_INTERVAL_MINUTES = 10

# A held capture that cannot be retried (provider unreachable, disk full) is
# finalized as it is once this long has passed since its last attempt, so it
# never sits unplayable indefinitely.
HELD_CAPTURE_MAX_WAIT_HOURS = 6
