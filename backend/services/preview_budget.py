"""The shared budget for provider preview connections.

Live, catchup and VOD previews all open a connection to the provider on the
user's behalf, and providers cap how many connections an account may hold at
once (three, on the accounts this was measured against — shared with any
running download). So the three preview paths draw on one budget rather than
one each: a viewer holding a live preview and a movie preview is two provider
connections for one person, and counting them separately would let a single
browser tab exhaust the account.

The budget is process-wide state, so it lives here rather than in whichever
router happened to need it first.
"""


# Two, deliberately: one preview plus headroom for a running download inside a
# three-connection provider allowance. Raising it means thinking about that
# allowance, not just about this process.
PREVIEW_MAX_CONCURRENT = 2


class PreviewLimitError(Exception):
    """No preview slot is free."""


class PreviewBudget:
    """A counter of in-flight previews. Not a semaphore: a request that cannot
    have a slot is refused immediately rather than queued behind someone
    else's preview."""

    def __init__(self, limit: int):
        self.limit = limit
        self.active = 0

    def acquire(self) -> None:
        if self.active >= self.limit:
            raise PreviewLimitError(
                "Preview limit reached. Close another preview and try again."
            )
        self.active += 1

    def release(self) -> None:
        # Clamped: releases arrive from teardown paths that are deliberately
        # idempotent, and a negative count would hand out a slot twice.
        self.active = max(0, self.active - 1)


preview_budget = PreviewBudget(PREVIEW_MAX_CONCURRENT)
