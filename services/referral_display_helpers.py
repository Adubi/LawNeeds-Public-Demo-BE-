"""
Shared display-formatting helpers for referral field values.

These are used by both the HTTP layer (api/inbox.py) and the services layer
(referral_user_status_payload.py / WebSocket pushes) so that every consumer
returns human-friendly, consistently formatted values.
"""
from typing import Optional


def get_budget_range_display(key: Optional[str]) -> str:
    """Convert a budget_range DB value to a human-friendly string with £."""
    if not key:
        return "Not specified"

    # Specific numeric amount (e.g. stored as "350")
    if key.isdigit():
        return f"£{key}"

    mapping = {
        "below_300":  "Below £300",
        # Canonical spellings (referral.py / models.py)
        "300_to_499": "£300 - £500",
        "500_to_699": "£500 - £700",
        # Variant spellings used by app.py — kept as aliases so existing DB rows display correctly
        "300_to_500": "£300 - £500",
        "500_to_700": "£500 - £700",
        "above_700":  "£700+",
    }
    return mapping.get(key, key)


def get_urgency_level_display(key: Optional[str]) -> str:
    """Convert an urgency_level DB value to a human-friendly string."""
    if not key:
        return "Not specified"
    mapping = {
        "urgent": "Urgent",
        "normal": "Normal",
        "flexible": "Flexible",
        "inferred_urgent": "Urgent (AI detected)",
        "within_week": "Within a week",
        "within_month": "Within a month",
        "not_urgent": "Not urgent",
    }
    return mapping.get(key, key)


def get_consultation_duration_display(consultation_category_key: Optional[str]) -> str:
    """Convert a consultation_category DB value to a human-friendly string."""
    if not consultation_category_key:
        return "Not specified"
    mapping = {
        "single_consultation": "Single Consultation",
        "ongoing_support": "Ongoing Support",
        "casework": "Casework",
        "not_sure": "Not Sure Yet",
    }
    return mapping.get(consultation_category_key, consultation_category_key)


def get_consultation_type_display(consultation_type_key: Optional[str]) -> str:
    """Convert a consultation_type DB value to a human-friendly string."""
    if not consultation_type_key:
        return "Not specified"
    mapping = {
        "single_5_min": "5 minutes",
        "single_10_min": "10 minutes",
        "single_20_min": "20 minutes",
        "single_30_min": "30 minutes",
        "single_1_hour": "1 hour consultation",
        "ongoing_3_day": "3 day ongoing support",
        "ongoing_5_day": "5 day ongoing support",
        "casework": "Casework",
        "not_sure": "Not sure yet",
    }
    return mapping.get(consultation_type_key, consultation_type_key)
