"""
Call entitlements config — Phase 1 of LawNeeds in-app voice/video calling.

Maps a ReferralRequest.consultation_type value to whether the user is
entitled to voice and/or video calling for that consultation.

This is the single source of truth for call entitlement. Both the
user-facing and professional-facing apps must read entitlement from the
backend (never infer client-side) - see Section 3 / 7.2 of the calling
scope doc. Changing what a package gets means editing this dict only;
no endpoint or screen should hard-code these rules.

Confirmed rule: video is never granted without voice also being granted.
"""

from services.models import ConsultationType


# consultation_type value -> {"voice": bool, "video": bool}
CALL_ENTITLEMENTS: dict[str, dict[str, bool]] = {
    # Single session consultations - voice only, regardless of length.
    # NOTE: the original scope doc's Section 3 table only explicitly listed
    # 20/30/60 min. Confirmed with Femi (2026-09-11) that 5/10 min sessions
    # get the same treatment as 20/30/60 min: voice yes, video no.
    ConsultationType.SINGLE_5_MIN.value: {"voice": True, "video": False},
    ConsultationType.SINGLE_10_MIN.value: {"voice": True, "video": False},
    ConsultationType.SINGLE_20_MIN.value: {"voice": True, "video": False},
    ConsultationType.SINGLE_30_MIN.value: {"voice": True, "video": False},
    ConsultationType.SINGLE_1_HOUR.value: {"voice": True, "video": False},

    # Ongoing Guided Support - voice and video.
    ConsultationType.ONGOING_3_DAY.value: {"voice": True, "video": True},
    ConsultationType.ONGOING_5_DAY.value: {"voice": True, "video": True},

    # Casework - voice and video.
    ConsultationType.CASEWORK.value: {"voice": True, "video": True},

    # Not yet decided on a package - no calling entitlement.
    ConsultationType.NOT_SURE.value: {"voice": False, "video": False},
}

# Fallback for any consultation_type not present in the map above
# (e.g. new types added later without updating this config).
DEFAULT_ENTITLEMENT: dict[str, bool] = {"voice": False, "video": False}


def get_call_entitlement(consultation_type: str) -> dict[str, bool]:
    """
    Return {"voice": bool, "video": bool} for a given consultation_type value.

    Always returns a dict (never raises) - unknown/new consultation types
    default to no entitlement rather than erroring, so a bad/unexpected
    value fails closed instead of accidentally granting access.
    """
    return CALL_ENTITLEMENTS.get(consultation_type, DEFAULT_ENTITLEMENT)
