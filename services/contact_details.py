"""Detection and enforcement for contact details shared in consultation chat."""

from __future__ import annotations

import re

from fastapi import HTTPException


CONTACT_DETAILS_ERROR = (
    "Phone numbers and email addresses cannot be shared in consultation messages. "
    "Please keep communication within LawNeeds."
)

# Supports normal addresses and common human-readable forms such as
# ``name [at] example [dot] com`` without attempting to classify every use of
# the words "at" and "dot" as an email address.
_EMAIL_RE = re.compile(
    r"(?ix)"
    r"(?<![\w.+-])"
    r"[a-z0-9][a-z0-9._%+-]{0,63}"
    r"\s*(?:@|\[\s*at\s*\]|\(\s*at\s*\)|\bat\b)\s*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\s*(?:\.|\[\s*dot\s*\]|\(\s*dot\s*\)|\bdot\b)\s*"
    r"[a-z]{2,24})+"
    r"(?![\w.-])"
)

# Candidate matching is deliberately permissive about separators. Candidates
# are accepted as phone numbers only after their digit count is checked below.
_PHONE_CANDIDATE_RE = re.compile(
    r"(?<![\w])(?:\+?\d[\d\s().-]{5,}\d)(?![\w])"
)


def contact_detail_types(text: str | None) -> list[str]:
    """Return the contact-detail types present in *text*."""

    if not text:
        return []

    violations: list[str] = []
    if _EMAIL_RE.search(text):
        violations.append("email")

    for candidate in _PHONE_CANDIDATE_RE.findall(text):
        digit_count = sum(character.isdigit() for character in candidate)
        minimum_digits = 7 if candidate.lstrip().startswith("+") else 10
        if minimum_digits <= digit_count <= 15:
            violations.append("phone")
            break

    return violations


def contains_contact_details(text: str | None) -> bool:
    return bool(contact_detail_types(text))


def assert_no_contact_details(text: str | None) -> None:
    """Raise a stable API error when a message contains contact details."""

    if contains_contact_details(text):
        raise HTTPException(status_code=400, detail=CONTACT_DETAILS_ERROR)
