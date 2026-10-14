"""Featured / branch-out lawyer recommendation system.

To add or remove featured lawyers, edit FEATURED_LAWYER_UUIDS below.
To disable the entire feature (revert to pure score-based recommendations),
set FEATURED_LAWYERS_ENABLED = False.

When enabled, featured lawyers are guaranteed a slot at the top of every
recommendation list (up to the total cap of 3).  Any remaining slots are
filled by the normal score-based algorithm, excluding already-featured
lawyers so there are no duplicates.

Ordering within the featured group preserves the list order defined here,
so the first UUID always receives is_best_suited = True.
"""

import logging
import uuid as _uuid
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─── Toggle ───────────────────────────────────────────────────────────────────

FEATURED_LAWYERS_ENABLED: bool = False

# ─── Featured lawyer UUIDs ────────────────────────────────────────────────────
# Add more UUIDs here as needed.  Order determines recommendation rank.

FEATURED_LAWYER_UUIDS: List[str] = [
    #"32295fe8-7fbf-4001-873c-201e7066101b",
    #"45c8c549-1fdd-4463-8a07-3d1878e20f22",
    "d854f0db-5bd3-4172-824b-95ae5c7ffee3",
    "05e2b6b4-3af8-4da3-aada-29512eb18139",
    "45c204f8-23a3-4dc9-b012-67b6dde873aa",
]

# ─── Internal helpers ─────────────────────────────────────────────────────────

def _parse_uuids() -> List[_uuid.UUID]:
    """Return FEATURED_LAWYER_UUIDS as a list of UUID objects, skipping malformed entries."""
    parsed: List[_uuid.UUID] = []
    for raw in FEATURED_LAWYER_UUIDS:
        try:
            parsed.append(_uuid.UUID(raw))
        except (ValueError, AttributeError):
            logger.warning("featured_lawyers: invalid UUID skipped: %r", raw)
    return parsed


def is_enabled() -> bool:
    """Return True when the featured-lawyer system is active."""
    return FEATURED_LAWYERS_ENABLED


def get_featured_uuids() -> List[_uuid.UUID]:
    """Return the active featured UUIDs, or an empty list when disabled."""
    if not FEATURED_LAWYERS_ENABLED:
        return []
    return _parse_uuids()


def fetch_featured_lawyers(session, User, LawyerAvailability) -> List[Tuple]:
    """Query the DB and return (User, LawyerAvailability | None) tuples for
    each featured UUID that exists and is of professional_type 'Lawyer'.

    Results are returned in the same order as FEATURED_LAWYER_UUIDS so that
    ranking is deterministic and controlled purely by the list above.
    """
    if not FEATURED_LAWYERS_ENABLED:
        return []

    featured_uuids = _parse_uuids()
    if not featured_uuids:
        return []

    rows = (
        session.query(User, LawyerAvailability)
        .outerjoin(LawyerAvailability, LawyerAvailability.lawyer_id == User.id)
        .filter(
            User.id.in_(featured_uuids),
            User.professional_type == "Lawyer",
            User.is_search_visible.is_(True),
        )
        .all()
    )

    # Re-order rows to match FEATURED_LAWYER_UUIDS order
    index_map = {uid: pos for pos, uid in enumerate(featured_uuids)}
    rows.sort(key=lambda pair: index_map.get(pair[0].id, 999))

    if len(rows) < len(featured_uuids):
        found_ids = {str(pair[0].id) for pair in rows}
        missing = [u for u in FEATURED_LAWYER_UUIDS if u not in found_ids]
        logger.warning(
            "featured_lawyers: %d UUID(s) not found in DB or not of type Lawyer: %s",
            len(missing),
            missing,
        )

    return rows
