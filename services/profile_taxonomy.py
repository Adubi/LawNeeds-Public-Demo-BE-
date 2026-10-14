"""
Stable integer IDs for legal_expertise, services, skills, practice_areas, areas_of_legal_focus.

FE sends/receives IDs. The DB VARCHAR[] columns keep storing canonical English labels
so referral, inbox, and other untouched code keeps working.
"""
from enum import IntEnum
from typing import Dict, List, Optional, Sequence, Set, Tuple

from fastapi import HTTPException


# ── Practice Areas (used by legal_expertise, practice_areas, areas_of_legal_focus) ──

class PracticeAreaID(IntEnum):
    IMMIGRATION_LAW = 1
    EMPLOYMENT_LAW = 2
    ROAD_TRAFFIC_OFFENCES = 3
    FAMILY_LAW = 4
    CORPORATE_LAW = 5
    TAX_LAW = 6
    CONTRACT_LAW = 7
    LITIGATION = 8
    CRIMINAL_LAW = 9
    PERSONAL_INJURY = 10
    PROPERTY_LAW = 11
    INTELLECTUAL_PROPERTY = 12
    MEDIATION = 13
    ARBITRATION = 14
    LEGAL_CONSULTATION = 15
    DOCUMENT_REVIEW = 16
    LEGAL_RESEARCH = 17
    COMPLIANCE = 18
    MERGERS_AND_ACQUISITIONS = 19
    BANKING_AND_FINANCE = 20
    INSURANCE_LAW = 21
    ENVIRONMENTAL_LAW = 22
    HUMAN_RIGHTS_LAW = 23
    INTERNATIONAL_LAW = 24
    ADMINISTRATIVE_LAW = 25
    CONSTITUTIONAL_LAW = 26


_PRACTICE_AREA_LABELS: Dict[PracticeAreaID, str] = {
    PracticeAreaID.IMMIGRATION_LAW: "Immigration Law",
    PracticeAreaID.EMPLOYMENT_LAW: "Employment Law",
    PracticeAreaID.ROAD_TRAFFIC_OFFENCES: "Road Traffic Offences",
    PracticeAreaID.FAMILY_LAW: "Family Law",
    PracticeAreaID.CORPORATE_LAW: "Corporate Law",
    PracticeAreaID.TAX_LAW: "Tax Law",
    PracticeAreaID.CONTRACT_LAW: "Contract Law",
    PracticeAreaID.LITIGATION: "Litigation",
    PracticeAreaID.CRIMINAL_LAW: "Criminal Law",
    PracticeAreaID.PERSONAL_INJURY: "Personal Injury",
    PracticeAreaID.PROPERTY_LAW: "Property Law",
    PracticeAreaID.INTELLECTUAL_PROPERTY: "Intellectual Property",
    PracticeAreaID.MEDIATION: "Mediation",
    PracticeAreaID.ARBITRATION: "Arbitration",
    PracticeAreaID.LEGAL_CONSULTATION: "Legal Consultation",
    PracticeAreaID.DOCUMENT_REVIEW: "Document Review",
    PracticeAreaID.LEGAL_RESEARCH: "Legal Research",
    PracticeAreaID.COMPLIANCE: "Compliance",
    PracticeAreaID.MERGERS_AND_ACQUISITIONS: "Mergers & Acquisitions",
    PracticeAreaID.BANKING_AND_FINANCE: "Banking & Finance",
    PracticeAreaID.INSURANCE_LAW: "Insurance Law",
    PracticeAreaID.ENVIRONMENTAL_LAW: "Environmental Law",
    PracticeAreaID.HUMAN_RIGHTS_LAW: "Human Rights Law",
    PracticeAreaID.INTERNATIONAL_LAW: "International Law",
    PracticeAreaID.ADMINISTRATIVE_LAW: "Administrative Law",
    PracticeAreaID.CONSTITUTIONAL_LAW: "Constitutional Law",
}


# ── Legal Services ──
# IDs are intentionally aligned with PracticeAreaID (1-26) so that both
# /legal-services and /practice-areas share the same numeric ID space and the
# app can use a single translation table for both.

class LegalServiceID(IntEnum):
    IMMIGRATION_LAW = 1
    EMPLOYMENT_LAW = 2
    ROAD_TRAFFIC_OFFENCES = 3
    FAMILY_LAW = 4
    CORPORATE_LAW = 5
    TAX_LAW = 6
    CONTRACT_LAW = 7
    LITIGATION = 8
    CRIMINAL_LAW = 9
    PERSONAL_INJURY = 10
    PROPERTY_LAW = 11
    INTELLECTUAL_PROPERTY = 12
    MEDIATION = 13
    ARBITRATION = 14
    LEGAL_CONSULTATION = 15
    DOCUMENT_REVIEW = 16
    LEGAL_RESEARCH = 17
    COMPLIANCE = 18
    MERGERS_AND_ACQUISITIONS = 19
    BANKING_AND_FINANCE = 20
    INSURANCE_LAW = 21
    ENVIRONMENTAL_LAW = 22
    HUMAN_RIGHTS_LAW = 23
    INTERNATIONAL_LAW = 24
    ADMINISTRATIVE_LAW = 25
    CONSTITUTIONAL_LAW = 26


_SERVICE_LABELS: Dict[LegalServiceID, str] = {
    LegalServiceID.IMMIGRATION_LAW: "Immigration Law",
    LegalServiceID.EMPLOYMENT_LAW: "Employment Law",
    LegalServiceID.ROAD_TRAFFIC_OFFENCES: "Road Traffic Offences",
    LegalServiceID.FAMILY_LAW: "Family Law",
    LegalServiceID.CORPORATE_LAW: "Corporate Law",
    LegalServiceID.TAX_LAW: "Tax Law",
    LegalServiceID.CONTRACT_LAW: "Contract Law",
    LegalServiceID.LITIGATION: "Litigation",
    LegalServiceID.CRIMINAL_LAW: "Criminal Law",
    LegalServiceID.PERSONAL_INJURY: "Personal Injury",
    LegalServiceID.PROPERTY_LAW: "Property Law",
    LegalServiceID.INTELLECTUAL_PROPERTY: "Intellectual Property",
    LegalServiceID.MEDIATION: "Mediation",
    LegalServiceID.ARBITRATION: "Arbitration",
    LegalServiceID.LEGAL_CONSULTATION: "Legal Consultation",
    LegalServiceID.DOCUMENT_REVIEW: "Document Review",
    LegalServiceID.LEGAL_RESEARCH: "Legal Research",
    LegalServiceID.COMPLIANCE: "Compliance",
    LegalServiceID.MERGERS_AND_ACQUISITIONS: "Mergers & Acquisitions",
    LegalServiceID.BANKING_AND_FINANCE: "Banking & Finance",
    LegalServiceID.INSURANCE_LAW: "Insurance Law",
    LegalServiceID.ENVIRONMENTAL_LAW: "Environmental Law",
    LegalServiceID.HUMAN_RIGHTS_LAW: "Human Rights Law",
    LegalServiceID.INTERNATIONAL_LAW: "International Law",
    LegalServiceID.ADMINISTRATIVE_LAW: "Administrative Law",
    LegalServiceID.CONSTITUTIONAL_LAW: "Constitutional Law",
}


# ── Legal Skills ──

class LegalSkillID(IntEnum):
    LEGAL_RESEARCH = 2000
    LEGAL_WRITING = 2001
    CASE_ANALYSIS = 2002
    CONTRACT_DRAFTING = 2003
    NEGOTIATION = 2004
    CLIENT_COUNSELING = 2005
    COURT_ADVOCACY = 2006
    MEDIATION = 2007
    ARBITRATION = 2008
    DUE_DILIGENCE = 2009
    COMPLIANCE_REVIEW = 2010
    RISK_ASSESSMENT = 2011
    LEGAL_STRATEGY = 2012
    CROSS_EXAMINATION = 2013
    BRIEF_WRITING = 2014
    DEPOSITION = 2015
    DISCOVERY = 2016
    SETTLEMENT_NEGOTIATION = 2017
    REGULATORY_ANALYSIS = 2018
    INTERNATIONAL_LAW = 2019
    INTELLECTUAL_PROPERTY = 2020
    MERGERS_AND_ACQUISITIONS = 2021
    CORPORATE_GOVERNANCE = 2022
    EMPLOYMENT_RELATIONS = 2023
    IMMIGRATION_PROCEDURES = 2024


_SKILL_LABELS: Dict[LegalSkillID, str] = {
    LegalSkillID.LEGAL_RESEARCH: "Legal Research",
    LegalSkillID.LEGAL_WRITING: "Legal Writing",
    LegalSkillID.CASE_ANALYSIS: "Case Analysis",
    LegalSkillID.CONTRACT_DRAFTING: "Contract Drafting",
    LegalSkillID.NEGOTIATION: "Negotiation",
    LegalSkillID.CLIENT_COUNSELING: "Client Counseling",
    LegalSkillID.COURT_ADVOCACY: "Court Advocacy",
    LegalSkillID.MEDIATION: "Mediation",
    LegalSkillID.ARBITRATION: "Arbitration",
    LegalSkillID.DUE_DILIGENCE: "Due Diligence",
    LegalSkillID.COMPLIANCE_REVIEW: "Compliance Review",
    LegalSkillID.RISK_ASSESSMENT: "Risk Assessment",
    LegalSkillID.LEGAL_STRATEGY: "Legal Strategy",
    LegalSkillID.CROSS_EXAMINATION: "Cross-examination",
    LegalSkillID.BRIEF_WRITING: "Brief Writing",
    LegalSkillID.DEPOSITION: "Deposition",
    LegalSkillID.DISCOVERY: "Discovery",
    LegalSkillID.SETTLEMENT_NEGOTIATION: "Settlement Negotiation",
    LegalSkillID.REGULATORY_ANALYSIS: "Regulatory Analysis",
    LegalSkillID.INTERNATIONAL_LAW: "International Law",
    LegalSkillID.INTELLECTUAL_PROPERTY: "Intellectual Property",
    LegalSkillID.MERGERS_AND_ACQUISITIONS: "Mergers & Acquisitions",
    LegalSkillID.CORPORATE_GOVERNANCE: "Corporate Governance",
    LegalSkillID.EMPLOYMENT_RELATIONS: "Employment Relations",
    LegalSkillID.IMMIGRATION_PROCEDURES: "Immigration Procedures",
}


# ── Reverse lookups: label (case-insensitive) → id ──

_LABEL_TO_PRACTICE: Dict[str, int] = {v.lower(): k.value for k, v in _PRACTICE_AREA_LABELS.items()}
_LABEL_TO_SERVICE: Dict[str, int] = {v.lower(): k.value for k, v in _SERVICE_LABELS.items()}
_LABEL_TO_SKILL: Dict[str, int] = {v.lower(): k.value for k, v in _SKILL_LABELS.items()}

# Aliases for legacy strings stored in DB that differ from the canonical label
_PRACTICE_ALIASES: Dict[str, int] = {
    "immigration law": PracticeAreaID.IMMIGRATION_LAW,
    "immigration": PracticeAreaID.IMMIGRATION_LAW,
    "immigration law": PracticeAreaID.IMMIGRATION_LAW,
    "employment law": PracticeAreaID.EMPLOYMENT_LAW,
    "employment": PracticeAreaID.EMPLOYMENT_LAW,
    "family law": PracticeAreaID.FAMILY_LAW,
    "family": PracticeAreaID.FAMILY_LAW,
    "road traffic offences": PracticeAreaID.ROAD_TRAFFIC_OFFENCES,
    "road traffic law": PracticeAreaID.ROAD_TRAFFIC_OFFENCES,
    "traffic offences": PracticeAreaID.ROAD_TRAFFIC_OFFENCES,
    "criminal law": PracticeAreaID.CRIMINAL_LAW,
    "criminal": PracticeAreaID.CRIMINAL_LAW,
    "property law": PracticeAreaID.PROPERTY_LAW,
    "property": PracticeAreaID.PROPERTY_LAW,
    "corporate law": PracticeAreaID.CORPORATE_LAW,
    "corporate": PracticeAreaID.CORPORATE_LAW,
    "personal injury": PracticeAreaID.PERSONAL_INJURY,
    "tax law": PracticeAreaID.TAX_LAW,
}
_LABEL_TO_PRACTICE.update(_PRACTICE_ALIASES)


# ── Validation helpers ──

def validate_practice_ids(ids: Sequence[int]) -> None:
    for i in ids:
        try:
            PracticeAreaID(i)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid practice area id: {i}")


def validate_service_ids(ids: Sequence[int]) -> None:
    for i in ids:
        try:
            LegalServiceID(i)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid service id: {i}")


def validate_skill_ids(ids: Sequence[int]) -> None:
    for i in ids:
        try:
            LegalSkillID(i)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid skill id: {i}")


# ── ID → label ──

def practice_id_to_label(pid: int) -> str:
    return _PRACTICE_AREA_LABELS[PracticeAreaID(pid)]

def service_id_to_label(sid: int) -> str:
    return _SERVICE_LABELS[LegalServiceID(sid)]

def skill_id_to_label(kid: int) -> str:
    return _SKILL_LABELS[LegalSkillID(kid)]


def ids_to_labels(ids: Sequence[int], lookup: Dict) -> List[str]:
    return [lookup[i] for i in _dedupe(ids)]


# ── Label → {id, label} item ──

def label_to_practice_item(label: str) -> dict:
    pid = _LABEL_TO_PRACTICE.get(label.strip().lower())
    if pid is not None:
        return {"id": int(pid), "label": _PRACTICE_AREA_LABELS[PracticeAreaID(pid)]}
    return {"id": None, "label": label}

def label_to_service_item(label: str) -> dict:
    sid = _LABEL_TO_SERVICE.get(label.strip().lower())
    if sid is not None:
        return {"id": int(sid), "label": _SERVICE_LABELS[LegalServiceID(sid)]}
    return {"id": None, "label": label}

def label_to_skill_item(label: str) -> dict:
    kid = _LABEL_TO_SKILL.get(label.strip().lower())
    if kid is not None:
        return {"id": int(kid), "label": _SKILL_LABELS[LegalSkillID(kid)]}
    return {"id": None, "label": label}


# ── Batch: stored string array → (deduped ids, [{id, label}]) ──

def _dedupe(ids: Sequence[int]) -> List[int]:
    seen: Set[int] = set()
    out: List[int] = []
    for i in ids:
        ii = int(i)
        if ii not in seen:
            seen.add(ii)
            out.append(ii)
    return out


def labels_to_practice_items(labels: Optional[Sequence[str]]) -> Tuple[List[int], List[dict]]:
    if not labels:
        return [], []
    ids: List[int] = []
    items: List[dict] = []
    seen_ids: Set[int] = set()
    seen_orphans: Set[str] = set()
    for raw in labels:
        if raw is None:
            continue
        item = label_to_practice_item(raw)
        pid = item["id"]
        if pid is not None and pid not in seen_ids:
            seen_ids.add(pid)
            ids.append(pid)
            items.append(item)
        elif pid is None:
            lk = raw.strip().lower()
            if lk not in seen_orphans:
                seen_orphans.add(lk)
                items.append(item)
    return ids, items


def labels_to_service_items(labels: Optional[Sequence[str]]) -> Tuple[List[int], List[dict]]:
    if not labels:
        return [], []
    ids: List[int] = []
    items: List[dict] = []
    seen_ids: Set[int] = set()
    seen_orphans: Set[str] = set()
    for raw in labels:
        if raw is None:
            continue
        item = label_to_service_item(raw)
        sid = item["id"]
        if sid is not None and sid not in seen_ids:
            seen_ids.add(sid)
            ids.append(sid)
            items.append(item)
        elif sid is None:
            lk = raw.strip().lower()
            if lk not in seen_orphans:
                seen_orphans.add(lk)
                items.append(item)
    return ids, items


def labels_to_skill_items(labels: Optional[Sequence[str]]) -> Tuple[List[int], List[dict]]:
    if not labels:
        return [], []
    ids: List[int] = []
    items: List[dict] = []
    seen_ids: Set[int] = set()
    seen_orphans: Set[str] = set()
    for raw in labels:
        if raw is None:
            continue
        item = label_to_skill_item(raw)
        kid = item["id"]
        if kid is not None and kid not in seen_ids:
            seen_ids.add(kid)
            ids.append(kid)
            items.append(item)
        elif kid is None:
            lk = raw.strip().lower()
            if lk not in seen_orphans:
                seen_orphans.add(lk)
                items.append(item)
    return ids, items


# ── IDs → canonical label strings (for writing to DB VARCHAR[]) ──

def practice_ids_to_labels(ids: Sequence[int]) -> List[str]:
    validate_practice_ids(ids)
    return [_PRACTICE_AREA_LABELS[PracticeAreaID(i)] for i in _dedupe(ids)]

def service_ids_to_labels(ids: Sequence[int]) -> List[str]:
    validate_service_ids(ids)
    return [_SERVICE_LABELS[LegalServiceID(i)] for i in _dedupe(ids)]

def skill_ids_to_labels(ids: Sequence[int]) -> List[str]:
    validate_skill_ids(ids)
    return [_SKILL_LABELS[LegalSkillID(i)] for i in _dedupe(ids)]


# ── Search filter resolvers (accept id OR legacy label string) ──

def resolve_practice_filter(raw: str) -> List[str]:
    """Resolve a practice_area query param (id like '1' or label like 'Immigration Law') to canonical labels."""
    s = (raw or "").strip()
    if not s:
        return []
    if s.isdigit():
        try:
            return [practice_id_to_label(int(s))]
        except (ValueError, KeyError):
            return []
    pid = _LABEL_TO_PRACTICE.get(s.lower())
    if pid is not None:
        return [_PRACTICE_AREA_LABELS[PracticeAreaID(pid)]]
    return [s]


def resolve_service_filters(parts: List[str]) -> List[str]:
    """Resolve comma-separated service query params (ids or labels) to canonical labels."""
    out: List[str] = []
    seen: Set[str] = set()
    for p in parts:
        x = p.strip()
        if not x:
            continue
        if x.isdigit():
            try:
                lbl = service_id_to_label(int(x))
            except (ValueError, KeyError):
                continue
        else:
            sid = _LABEL_TO_SERVICE.get(x.lower())
            lbl = _SERVICE_LABELS[LegalServiceID(sid)] if sid is not None else x
        lk = lbl.lower()
        if lk not in seen:
            seen.add(lk)
            out.append(lbl)
    return out


# ── Combined expertise payload for search results ──

def search_expertise_payload(prof) -> dict:
    """Merge legal_expertise + practice_areas + areas_of_legal_focus into deduped id-based output."""
    all_ids: Set[int] = set()
    all_items: Dict[int, str] = {}
    orphans: List[str] = []
    orphan_seen: Set[str] = set()

    for field in ("legal_expertise", "practice_areas", "areas_of_legal_focus"):
        arr = getattr(prof, field, None) or []
        ids, items = labels_to_practice_items(arr)
        for i in ids:
            all_ids.add(int(i))
            all_items[int(i)] = _PRACTICE_AREA_LABELS[PracticeAreaID(i)]
        for it in items:
            if it["id"] is None:
                lk = it["label"].lower()
                if lk not in orphan_seen:
                    orphan_seen.add(lk)
                    orphans.append(it["label"])

    ordered = sorted(all_ids)
    item_list = [{"id": i, "label": all_items[i]} for i in ordered]
    for ol in orphans:
        item_list.append({"id": None, "label": ol})

    return {"legal_expertise_ids": ordered, "legal_expertise": item_list}


# ── Catalog endpoints (returns full list for FE dropdowns) ──

def get_practice_area_catalog() -> List[dict]:
    return [{"id": int(e.value), "label": _PRACTICE_AREA_LABELS[e]} for e in PracticeAreaID]

def get_service_catalog() -> List[dict]:
    return [{"id": int(e.value), "label": _SERVICE_LABELS[e]} for e in LegalServiceID]

def get_skill_catalog() -> List[dict]:
    return [{"id": int(e.value), "label": _SKILL_LABELS[e]} for e in LegalSkillID]
