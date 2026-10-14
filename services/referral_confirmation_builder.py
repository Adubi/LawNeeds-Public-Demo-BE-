"""
Builder for referral confirmation parts.

Splits the referral confirmation message into three structured parts so
clients can render each independently (e.g. applying a blue border to the
description block) without brittle string parsing.

Output shape:
{
    "question":     "Awesome. So here's what I've got:\\n\\n<issue summary>\\n\\n",
    "description":  "Consultation type: ...\\nBudget: ...\\nUrgency: ...\\n[notes/attachments]\\n",
    "verification": "Is this correct?",
}

The full concatenated string (question + description + verification) is also
returned as ``full_summary`` so callers that still need the legacy single
string (e.g. persisting ``confirmation_summary`` for the lawyer inbox) can
keep using it without re-joining manually.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional


URGENCY_DISPLAY_MAP = {
    "urgent": "Urgent (within 24 hours)",
    "within_week": "Within a week",
    "within_month": "Within a month",
    "not_urgent": "Not urgent",
}

ISSUE_SUMMARY_MAX_LEN = 200
VERIFICATION_TEXT = "Is this correct?"


def _truncate_issue_summary(summary: str) -> str:
    if not summary:
        return ""
    if len(summary) > ISSUE_SUMMARY_MAX_LEN:
        return summary[:ISSUE_SUMMARY_MAX_LEN] + "..."
    return summary


def _format_urgency(level: Optional[str]) -> str:
    if not level:
        return "Not specified"
    return URGENCY_DISPLAY_MAP.get(level, level)


def _build_description_body(
    *,
    consultation_category: Optional[str],
    consultation_type: Optional[str],
    budget_range: Optional[str],
    urgency_display: str,
    additional_notes: str,
    attachment_lines: str,
    single_consultation_fragment: Optional[Callable[[str], str]] = None,
    casework_service_type: Optional[str] = None,
    casework_budget_gbp: Optional[int] = None,
    casework_price_label_fn: Optional[Callable[[Optional[str]], str]] = None,
) -> str:
    """Build the description block (details only, no intro, no verification)."""
    br = (budget_range or "").strip()

    if consultation_category == "single_consultation":
        fragment = (
            single_consultation_fragment(consultation_type or "")
            if single_consultation_fragment
            else (consultation_type or "")
        )
        description = (
            "Consultation type: Single consultation\n"
            f"Consultation time: {fragment or 'Not specified'}\n"
            f"Urgency: {urgency_display}"
        )
    elif consultation_type == "ongoing_3_day":
        budget_display = f"£{br}" if br.isdigit() else "£300–£500"
        description = (
            "Consultation type: Ongoing Support\n"
            "Consultation time: 3-Day Plan\n"
            f"Budget: {budget_display}\n"
            f"Urgency: {urgency_display}"
        )
    elif consultation_type == "ongoing_5_day":
        budget_display = f"£{br}" if br.isdigit() else "£500–£700"
        description = (
            "Consultation type: Ongoing Support\n"
            "Consultation time: 5-Day Plan\n"
            f"Budget: {budget_display}\n"
            f"Urgency: {urgency_display}"
        )
    elif consultation_type == "casework" or consultation_category == "casework":
        # Server's ``_create_referral_request`` normalizes a category of
        # "casework" to ``consultation_type = "casework"`` on insert, so the
        # confirmation block should render the same way whether the user has
        # already reached the specific-type step or is still in the category-
        # only state. When the new Service & Price flow has been used, surface
        # the chosen service and the exact £ figure the user committed to;
        # fall back to the legacy "Above £700" wording otherwise so older
        # referrals don't suddenly start showing blank fields.
        if casework_service_type and casework_price_label_fn:
            service_line = casework_price_label_fn(casework_service_type)
        elif casework_service_type:
            service_line = casework_service_type
        else:
            service_line = None

        if casework_budget_gbp:
            budget_display = f"£{casework_budget_gbp:,}"
        elif br.isdigit():
            budget_display = f"£{int(br):,}"
        else:
            budget_display = "Above £700"

        if service_line:
            description = (
                f"Consultation type: Casework — {service_line}\n"
                f"Budget: {budget_display}\n"
                f"Urgency: {urgency_display}"
            )
        else:
            description = (
                "Consultation type: Casework\n"
                f"Budget: {budget_display}\n"
                f"Urgency: {urgency_display}"
            )
    else:
        fallback_type = consultation_type or consultation_category or "General consultation"
        description = (
            f"Consultation type: {fallback_type}\n"
            f"Urgency: {urgency_display}"
        )

    if additional_notes and additional_notes.strip():
        description += f"\nAdditional notes: {additional_notes}"

    if attachment_lines:
        description += attachment_lines

    # Trailing newline so clients that concatenate parts get a clean break
    # before the verification line.
    if not description.endswith("\n"):
        description += "\n"

    return description


def build_referral_confirmation_parts(
    context: Dict[str, Any],
    *,
    attachment_confirmation_lines_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
    single_consultation_fragment_fn: Optional[Callable[[str], str]] = None,
    casework_price_label_fn: Optional[Callable[[Optional[str]], str]] = None,
) -> Dict[str, str]:
    """
    Build the three-part confirmation payload from a referral context dict.

    Parameters are injectable so this helper stays testable in isolation and
    does not pull the heavy ``upload_multipart`` / pricing modules unless
    needed by the caller.
    """
    issue_summary = _truncate_issue_summary(
        context.get("legal_matter_description") or context.get("issue_summary") or ""
    ) or "Legal assistance needed"

    consultation_category = context.get("consultation_category")
    consultation_type = context.get("consultation_type")
    budget_range = context.get("budget_range")
    urgency_level = context.get("urgency_level")
    additional_notes = context.get("additional_notes", "") or ""
    casework_service_type = context.get("casework_service_type")
    casework_budget_gbp = context.get("casework_budget_gbp")

    urgency_display = _format_urgency(urgency_level)

    attachment_lines = (
        attachment_confirmation_lines_fn(context) if attachment_confirmation_lines_fn else ""
    )

    question = f"Awesome. So here's what I've got:\n\n{issue_summary}\n\n"

    description = _build_description_body(
        consultation_category=consultation_category,
        consultation_type=consultation_type,
        budget_range=budget_range,
        urgency_display=urgency_display,
        additional_notes=additional_notes,
        attachment_lines=attachment_lines,
        single_consultation_fragment=single_consultation_fragment_fn,
        casework_service_type=casework_service_type,
        casework_budget_gbp=casework_budget_gbp,
        casework_price_label_fn=casework_price_label_fn,
    )

    verification = VERIFICATION_TEXT

    full_summary = f"{question}{description}\n{verification}"

    return {
        "question": question,
        "description": description,
        "verification": verification,
        "full_summary": full_summary,
        "issue_summary": issue_summary,
    }
