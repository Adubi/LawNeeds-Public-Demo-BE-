"""Lawyer referral response-rate and response-time calculations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from services.models import LawyerInbox


RESPONSE_STATS_WINDOW_DAYS = 90
RESPONDED_STATUSES = frozenset({"accepted", "rejected"})
ELIGIBLE_STATUSES = RESPONDED_STATUSES | {"expired"}


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_response_time(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return "Under a minute"
    if seconds < 3600:
        minutes = max(1, round(seconds / 60))
        return f"About {minutes} minute{'s' if minutes != 1 else ''}"
    if seconds < 86400:
        hours = max(1, round(seconds / 3600))
        return f"About {hours} hour{'s' if hours != 1 else ''}"
    days = max(1, round(seconds / 86400))
    return f"About {days} day{'s' if days != 1 else ''}"


def calculate_lawyer_response_stats(
    inbox_entries: Iterable[Any],
    *,
    window_days: int = RESPONSE_STATS_WINDOW_DAYS,
) -> dict[str, Any]:
    """Build public profile stats from sent and accept/reject timestamps.

    Pending/read requests are not counted until they expire, and referrals
    cancelled by a client are excluded. This prevents a newly received request
    from immediately lowering the lawyer's rate.
    """

    entries = [
        entry for entry in inbox_entries
        if (getattr(entry, "status", "") or "").lower() in ELIGIBLE_STATUSES
    ]
    responded = [
        entry for entry in entries
        if (getattr(entry, "status", "") or "").lower() in RESPONDED_STATUSES
        and getattr(entry, "responded_at", None) is not None
    ]

    eligible_count = len(entries)
    responded_count = len(responded)
    response_rate_percent = (
        round((responded_count / eligible_count) * 100, 1)
        if eligible_count
        else None
    )

    response_seconds: list[float] = []
    for entry in responded:
        created_at = getattr(entry, "created_at", None)
        responded_at = getattr(entry, "responded_at", None)
        if not created_at or not responded_at:
            continue
        elapsed = (_as_utc(responded_at) - _as_utc(created_at)).total_seconds()
        if elapsed >= 0:
            response_seconds.append(elapsed)

    average_seconds = (
        round(sum(response_seconds) / len(response_seconds))
        if response_seconds
        else None
    )

    return {
        "response_rate_percent": response_rate_percent,
        "average_response_time_seconds": average_seconds,
        "average_response_time_label": format_response_time(average_seconds),
        "responded_requests": responded_count,
        "eligible_requests": eligible_count,
        "window_days": window_days,
    }


def get_lawyer_response_stats(
    session,
    lawyer_id,
    *,
    now: datetime | None = None,
    window_days: int = RESPONSE_STATS_WINDOW_DAYS,
) -> dict[str, Any]:
    """Load and calculate a lawyer's rolling response statistics."""

    now = _as_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=window_days)
    entries = (
        session.query(LawyerInbox)
        .filter(
            LawyerInbox.lawyer_id == lawyer_id,
            LawyerInbox.created_at >= cutoff,
        )
        .all()
    )
    return calculate_lawyer_response_stats(entries, window_days=window_days)
