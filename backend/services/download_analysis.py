"""Compare raw catchup media with the duration that was actually requested.

This is evidence, not a completion or retry policy. Never replace an absent
measurement with the expected duration: that would turn an unknown recording
into an apparently complete one. Commercial Skip and other transformations
must not change this original observation.
"""
import math
from dataclasses import dataclass


DURATION_TOLERANCE_SECONDS = 5.0
DURATION_ANALYSIS_STATUSES = ("matched", "short", "long", "unavailable", "unreliable")


@dataclass(frozen=True)
class DurationAnalysis:
    expected_seconds: float
    actual_seconds: float | None
    status: str
    reason: str | None = None


def analyze_duration(expected_seconds: float, measured_seconds: float | None) -> DurationAnalysis:
    """Classify a probe result without confusing an estimate with certainty.

    Five seconds allows for packet/frame boundaries without hiding minutes of
    missing content. MPEG-TS timestamp resets can make an hour look like 26
    hours; unusually large estimates remain visible but cannot drive automatic
    file changes. The allowance is the larger of twice the request or an extra
    30 minutes, so a short request with a real five-minute tail is still usable.
    This is a sanity bound, not a full packet-continuity or content check.
    """
    if (
        isinstance(expected_seconds, bool)
        or not isinstance(expected_seconds, (int, float))
        or not math.isfinite(expected_seconds)
        or expected_seconds <= 0
    ):
        return DurationAnalysis(0, None, "unavailable", "The requested duration is unavailable.")
    if (
        isinstance(measured_seconds, bool)
        or not isinstance(measured_seconds, (int, float))
        or not math.isfinite(measured_seconds)
        or measured_seconds <= 0
    ):
        return DurationAnalysis(
            expected_seconds, None, "unavailable", "ffprobe could not measure the raw download duration."
        )
    if measured_seconds > max(expected_seconds * 2, expected_seconds + 1800):
        return DurationAnalysis(
            expected_seconds,
            measured_seconds,
            "unreliable",
            "The measured duration is implausibly large; the file may contain broken timestamps.",
        )
    difference = measured_seconds - expected_seconds
    status = "matched"
    if difference < -DURATION_TOLERANCE_SECONDS:
        status = "short"
    elif difference > DURATION_TOLERANCE_SECONDS:
        status = "long"
    return DurationAnalysis(expected_seconds, measured_seconds, status)
