"""
Display pricing for the referral flow — aligned with pricing.md
section "LAWYER CONSULTATION PRICING" (Single-Session Consultations table).

Used by api/referral.py for option labels and confirmation summaries.
DB `consultation_pricing.price_gbp` should match these values (see seed script).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# pricing.md: Type | Duration | Price | £/Min
_SINGLE_DURATION_ROWS: List[Dict[str, Any]] = [
    {
        "value": "single_5_min",
        "label": "Quick advice — 5 minutes (£20)",
        "price_gbp": "£20",
        "price_per_min_gbp": "£4.00/min",
    },
    {
        "value": "single_10_min",
        "label": "Standard — 10 minutes (£50)",
        "price_gbp": "£50",
        "price_per_min_gbp": "£5.00/min",
    },
    {
        "value": "single_20_min",
        "label": "Extended — 20 minutes (£70)",
        "price_gbp": "£70",
        "price_per_min_gbp": "£3.50/min",
    },
    {
        "value": "single_30_min",
        "label": "Full session — 30 minutes (£100)",
        "price_gbp": "£100",
        "price_per_min_gbp": "£3.33/min",
    },
    {
        "value": "single_1_hour",
        "label": "Flexible session — 1 hour (£150)",
        "price_gbp": "£150",
        "price_per_min_gbp": "£2.50/min",
    },
]

# Map consultation_type -> platform price string for summaries / validation messaging
SINGLE_CONSULTATION_PRICE_BY_TYPE: Dict[str, str] = {
    row["value"]: row["price_gbp"] for row in _SINGLE_DURATION_ROWS
}

# Short duration text for confirmation block
SINGLE_CONSULTATION_DURATION_LABEL: Dict[str, str] = {
    "single_5_min": "5 minutes",
    "single_10_min": "10 minutes",
    "single_20_min": "20 minutes",
    "single_30_min": "30 minutes",
    "single_1_hour": "1 hour",
}


def single_consultation_duration_options() -> List[Dict[str, Any]]:
    """Options for the referral flow duration step (copy for API responses)."""
    return [dict(row) for row in _SINGLE_DURATION_ROWS]


def consultation_category_options() -> List[Dict[str, Any]]:
    """Top-level consultation path choices with optional price hints for the UI."""
    return [
        {
            "value": "single_consultation",
            "label": "Single Consultation",
            "price_hint": "£20–£150 (fixed platform prices)",
        },
        {
            "value": "ongoing_support",
            "label": "Ongoing Support",
            "price_hint": "£300–£500 / £500–£700 plans (+£200 lawyer cap)",
        },
        {
            "value": "casework",
            "label": "Casework",
            "price_hint": "£850–£8,000+ (varies by service)",
        },
        {
            "value": "not_sure",
            "label": "Not Sure Yet",
            "price_hint": None,
        },
    ]


# ---------------------------------------------------------------------------
# Casework pricing — Service & Price column for the Casework path
# ---------------------------------------------------------------------------
#
# Each row defines:
#   value        : enum/storage value for the service
#   label        : human-readable name (matches the pricing table)
#   price_range  : display string shown next to the label
#   min          : minimum allowed budget (£). Users CANNOT pick below this.
#   max          : top of the published range (£) — used for hints.
#   budgets      : discrete picker values offered to the user. All values are
#                  >= min; the lowest entry equals min for every service.
#
_CASEWORK_SERVICE_ROWS: List[Dict[str, Any]] = [
    {
        "value": "skilled_worker_visa",
        "label": "Skilled Worker Visa",
        "price_range": "£1,600 – £2,300",
        "min": 1600, "max": 2300,
        "budgets": [1600, 1700, 1800, 1900, 2000, 2100, 2200, 2300],
    },
    {
        "value": "student_visa",
        "label": "Student Visa",
        "price_range": "£1,000 – £1,500",
        "min": 1000, "max": 1500,
        "budgets": [1000, 1100, 1200, 1300, 1400, 1500],
    },
    {
        "value": "visit_visa",
        "label": "Visit Visa",
        "price_range": "£850 – £1,300",
        "min": 850, "max": 1300,
        "budgets": [850, 900, 1000, 1100, 1200, 1300],
    },
    {
        "value": "ancestry_visa",
        "label": "Ancestry Visa",
        "price_range": "£1,800 – £2,400",
        "min": 1800, "max": 2400,
        "budgets": [1800, 1900, 2000, 2100, 2200, 2300, 2400],
    },
    {
        "value": "spouse_partner_visa",
        "label": "Spouse / Partner Visa",
        "price_range": "£2,200 – £4,500",
        "min": 2200, "max": 4500,
        "budgets": [2200, 2500, 3000, 3500, 4000, 4500],
    },
    {
        "value": "pbs_dependants",
        "label": "PBS Dependants",
        "price_range": "£1,300 – £2,000",
        "min": 1300, "max": 2000,
        "budgets": [1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000],
    },
    {
        "value": "family_reunion",
        "label": "Family Reunion",
        "price_range": "£2,200 – £3,500",
        "min": 2200, "max": 3500,
        "budgets": [2200, 2500, 2800, 3000, 3200, 3500],
    },
    {
        "value": "ilr_10_year",
        "label": "ILR (10-year route)",
        "price_range": "£2,200 – £5,000",
        "min": 2200, "max": 5000,
        "budgets": [2200, 2500, 3000, 3500, 4000, 4500, 5000],
    },
    {
        "value": "naturalisation",
        "label": "Naturalisation",
        "price_range": "£2,200 – £3,000",
        "min": 2200, "max": 3000,
        "budgets": [2200, 2400, 2600, 2800, 3000],
    },
    {
        "value": "eu_settlement_scheme",
        "label": "EU Settlement Scheme",
        "price_range": "£1,200 – £2,200",
        "min": 1200, "max": 2200,
        "budgets": [1200, 1400, 1600, 1800, 2000, 2200],
    },
    {
        "value": "flr_fp_human_rights",
        "label": "FLR(FP), Human Rights Applications",
        "price_range": "£2,500 – £5,500",
        "min": 2500, "max": 5500,
        "budgets": [2500, 3000, 3500, 4000, 4500, 5000, 5500],
    },
    {
        "value": "asylum_full_support",
        "label": "Asylum Applications (Full Support)",
        "price_range": "£3,000 – £6,500",
        "min": 3000, "max": 6500,
        "budgets": [3000, 3500, 4000, 4500, 5000, 5500, 6000, 6500],
    },
    {
        "value": "full_case_management",
        "label": "Full Case Management (End-to-End)",
        "price_range": "£3,000 – £8,000",
        "min": 3000, "max": 8000,
        "budgets": [3000, 4000, 5000, 6000, 7000, 8000],
    },
]

CASEWORK_SERVICE_VALUES: List[str] = [r["value"] for r in _CASEWORK_SERVICE_ROWS]
CASEWORK_PRICE_BY_SERVICE: Dict[str, str] = {
    r["value"]: r["price_range"] for r in _CASEWORK_SERVICE_ROWS
}
CASEWORK_LABEL_BY_SERVICE: Dict[str, str] = {
    r["value"]: r["label"] for r in _CASEWORK_SERVICE_ROWS
}
CASEWORK_MIN_BUDGET: Dict[str, int] = {r["value"]: r["min"] for r in _CASEWORK_SERVICE_ROWS}
CASEWORK_MAX_BUDGET: Dict[str, int] = {r["value"]: r["max"] for r in _CASEWORK_SERVICE_ROWS}
_CASEWORK_BUDGET_OPTIONS: Dict[str, List[int]] = {
    r["value"]: list(r["budgets"]) for r in _CASEWORK_SERVICE_ROWS
}


def casework_service_options() -> List[Dict[str, Any]]:
    """Dropdown options for the casework_service step.

    Mirrors ``single_consultation_duration_options()``: each option includes
    its label (with price range) and the price metadata so clients can render
    a "Service & Price" picker similar to "Select Time & Rate".
    """
    return [
        {
            "value": r["value"],
            "label": f"{r['label']} ({r['price_range']})",
            "service_label": r["label"],
            "price_range": r["price_range"],
            "min_budget_gbp": r["min"],
            "max_budget_gbp": r["max"],
        }
        for r in _CASEWORK_SERVICE_ROWS
    ]


def casework_budget_options(service_type: str) -> List[Dict[str, Any]]:
    """Discrete budget picker values for a given casework service type.

    Returns an empty list if the service type is unknown. The lowest amount
    in the list is always the service's minimum, so any selection from this
    list is guaranteed to satisfy the floor validation.
    """
    amounts = _CASEWORK_BUDGET_OPTIONS.get(service_type, [])
    return [{"value": str(amt), "label": f"£{amt:,}"} for amt in amounts]


def casework_price_label(service_type: Optional[str]) -> str:
    """Return ``"<Service Name> (£min – £max)"`` for confirmation summaries."""
    if not service_type or service_type not in CASEWORK_LABEL_BY_SERVICE:
        return service_type or ""
    return f"{CASEWORK_LABEL_BY_SERVICE[service_type]} ({CASEWORK_PRICE_BY_SERVICE[service_type]})"


def validate_casework_budget(service_type: str, amount_gbp: int) -> Optional[str]:
    """Return ``None`` if ``amount_gbp`` is allowed for the service, else an
    error message describing the violation. Used by both the chat referral
    flow and the request_service endpoint to enforce the per-service floor.
    """
    if service_type not in CASEWORK_MIN_BUDGET:
        return f"Unknown casework service '{service_type}'."
    min_gbp = CASEWORK_MIN_BUDGET[service_type]
    if amount_gbp < min_gbp:
        return (
            f"Budget £{amount_gbp:,} is below the minimum (£{min_gbp:,}) "
            f"for {CASEWORK_LABEL_BY_SERVICE[service_type]} "
            f"(estimated range: {CASEWORK_PRICE_BY_SERVICE[service_type]})."
        )
    return None


def single_consultation_confirmation_fragment(consultation_type: str) -> str:
    """Human-readable duration + platform price for confirm step."""
    duration = SINGLE_CONSULTATION_DURATION_LABEL.get(consultation_type, consultation_type)
    price = SINGLE_CONSULTATION_PRICE_BY_TYPE.get(consultation_type, "")
    if price:
        return f"{duration} - {price}"
    return duration


def seed_single_consultation_price_strings() -> Dict[str, str]:
    """price_gbp values for ConsultationPricing rows (numeric part only, with £)."""
    return {row["value"]: row["price_gbp"] for row in _SINGLE_DURATION_ROWS}
