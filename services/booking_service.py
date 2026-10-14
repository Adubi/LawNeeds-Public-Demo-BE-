import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.models import (
    ConsultationBookingRequest,
    ConsultationBookingStatus,
    ProfessionalAvailabilityException,
    ProfessionalAvailabilityRule,
    ProfessionalPresence,
)

CONSULTATION_TYPE_DURATIONS = {
    "single_5_min": 5,
    "single_10_min": 10,
    "single_20_min": 20,
    "single_30_min": 30,
    "single_1_hour": 60,
    "ongoing_3_day": 60,
    "ongoing_5_day": 60,
}

BOOKING_CONFLICT_STATUSES = {
    ConsultationBookingStatus.PENDING.value,
    ConsultationBookingStatus.ACCEPTED.value,
}


@dataclass(frozen=True)
class BookingSlot:
    start_at: datetime
    end_at: datetime
    timezone: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "start_at": self.start_at.isoformat(),
            "end_at": self.end_at.isoformat(),
            "timezone": self.timezone,
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_datetime(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an ISO-8601 datetime")
    return ensure_utc(parsed)


def parse_timezone(tz_name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "Europe/London")
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown timezone: {tz_name}")


def consultation_duration_minutes(consultation_type: str) -> int:
    return CONSULTATION_TYPE_DURATIONS.get(consultation_type, 30)


def requires_booking_slot(consultation_category: str) -> bool:
    return consultation_category in {"single_consultation", "ongoing_support"}


def effective_presence_status(
    presence: Optional[ProfessionalPresence],
    *,
    accepting_consultations: bool = True,
    now: Optional[datetime] = None,
) -> Tuple[str, str]:
    now = ensure_utc(now or utc_now())

    if not accepting_consultations:
        return "offline", "Offline"

    if not presence:
        return "offline", "Offline"

    if presence.manual_status_override == "offline":
        return "offline", "Offline"
    if presence.manual_status_override == "away":
        return "away", "Away"
    if not presence.accepting_consultations:
        return "offline", "Offline"

    heartbeat = presence.last_heartbeat_at
    if not heartbeat:
        return "offline", "Offline"

    age = now - ensure_utc(heartbeat)
    if age <= timedelta(minutes=2):
        return "online", "Online"
    if age <= timedelta(minutes=15):
        return "away", "Away"
    return "offline", "Offline"


class BookingService:
    """LawNeeds booking source of truth."""

    def __init__(self, session):
        self.session = session

    def get_availability(self, professional_id: uuid.UUID) -> Dict[str, Any]:
        rules = (
            self.session.query(ProfessionalAvailabilityRule)
            .filter(ProfessionalAvailabilityRule.professional_id == professional_id)
            .order_by(ProfessionalAvailabilityRule.weekday, ProfessionalAvailabilityRule.start_minute)
            .all()
        )
        exceptions = (
            self.session.query(ProfessionalAvailabilityException)
            .filter(ProfessionalAvailabilityException.professional_id == professional_id)
            .order_by(ProfessionalAvailabilityException.start_at)
            .all()
        )
        return {
            "rules": [self._rule_to_dict(rule) for rule in rules],
            "exceptions": [self._exception_to_dict(exc) for exc in exceptions],
        }

    def replace_availability(
        self,
        professional_id: uuid.UUID,
        *,
        rules: List[Dict[str, Any]],
        exceptions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        self.session.query(ProfessionalAvailabilityRule).filter(
            ProfessionalAvailabilityRule.professional_id == professional_id
        ).delete(synchronize_session=False)
        self.session.query(ProfessionalAvailabilityException).filter(
            ProfessionalAvailabilityException.professional_id == professional_id
        ).delete(synchronize_session=False)

        for raw in rules:
            self.session.add(self._build_rule(professional_id, raw))
        for raw in exceptions:
            self.session.add(self._build_exception(professional_id, raw))
        self.session.flush()
        return self.get_availability(professional_id)

    def generate_slots(
        self,
        professional_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
        consultation_type: str,
    ) -> List[BookingSlot]:
        start_at = ensure_utc(start_at)
        end_at = ensure_utc(end_at)
        if end_at <= start_at:
            raise ValueError("to must be after from")

        duration = timedelta(minutes=consultation_duration_minutes(consultation_type))
        windows = self._candidate_windows(
            professional_id,
            start_at=start_at,
            end_at=end_at,
            consultation_type=consultation_type,
        )
        blockers = self._blocking_windows(professional_id, start_at=start_at, end_at=end_at)
        slots: List[BookingSlot] = []

        for window_start, window_end, tz_name in windows:
            cursor = window_start
            window_end = min(window_end, end_at)
            if cursor.second or cursor.microsecond:
                cursor = cursor.replace(second=0, microsecond=0) + timedelta(minutes=1)
            while cursor + duration <= window_end:
                slot_end = cursor + duration
                if cursor >= start_at and not self._overlaps_any(cursor, slot_end, blockers):
                    slots.append(BookingSlot(cursor, slot_end, tz_name))
                cursor += duration

        deduped: Dict[Tuple[datetime, datetime], BookingSlot] = {}
        for slot in slots:
            deduped[(slot.start_at, slot.end_at)] = slot
        return sorted(deduped.values(), key=lambda slot: slot.start_at)

    def validate_slot(
        self,
        professional_id: uuid.UUID,
        *,
        requested_start_at: datetime,
        requested_end_at: datetime,
        consultation_type: str,
    ) -> bool:
        requested_start_at = ensure_utc(requested_start_at)
        requested_end_at = ensure_utc(requested_end_at)
        if requested_end_at <= requested_start_at:
            return False

        duration = timedelta(minutes=consultation_duration_minutes(consultation_type))
        if requested_end_at - requested_start_at != duration:
            return False

        slots = self.generate_slots(
            professional_id,
            start_at=requested_start_at - timedelta(days=1),
            end_at=requested_end_at + timedelta(days=1),
            consultation_type=consultation_type,
        )
        return any(
            slot.start_at == requested_start_at and slot.end_at == requested_end_at
            for slot in slots
        )

    def create_booking_request(
        self,
        *,
        user_id: uuid.UUID,
        professional_id: uuid.UUID,
        referral_request_id: uuid.UUID,
        consultation_category: str,
        consultation_type: str,
        requested_start_at: datetime,
        requested_end_at: datetime,
        timezone_name: str,
    ) -> ConsultationBookingRequest:
        booking = ConsultationBookingRequest(
            user_id=user_id,
            professional_id=professional_id,
            referral_request_id=referral_request_id,
            consultation_category=consultation_category,
            consultation_type=consultation_type,
            requested_start_at=ensure_utc(requested_start_at),
            requested_end_at=ensure_utc(requested_end_at),
            timezone=timezone_name or "Europe/London",
            status=ConsultationBookingStatus.PENDING.value,
        )
        self.session.add(booking)
        self.session.flush()
        return booking

    def update_booking_for_referral(self, referral_request_id: uuid.UUID, status: str) -> Optional[ConsultationBookingRequest]:
        booking = (
            self.session.query(ConsultationBookingRequest)
            .filter(ConsultationBookingRequest.referral_request_id == referral_request_id)
            .first()
        )
        if not booking:
            return None

        booking.status = status
        booking.updated_at = utc_now()
        if status == ConsultationBookingStatus.ACCEPTED.value:
            booking.accepted_at = utc_now()
        if status in {
            ConsultationBookingStatus.REJECTED.value,
            ConsultationBookingStatus.CANCELLED.value,
            ConsultationBookingStatus.EXPIRED.value,
        }:
            booking.cancelled_at = utc_now()
        return booking

    def mark_paid_for_referral(self, referral_request_id: uuid.UUID) -> Optional[ConsultationBookingRequest]:
        booking = (
            self.session.query(ConsultationBookingRequest)
            .filter(ConsultationBookingRequest.referral_request_id == referral_request_id)
            .first()
        )
        if not booking:
            return None

        if booking.status == ConsultationBookingStatus.PENDING.value:
            booking.status = ConsultationBookingStatus.ACCEPTED.value
            booking.accepted_at = utc_now()
        return booking

    def next_available_at(
        self,
        professional_id: uuid.UUID,
        *,
        consultation_type: str = "single_30_min",
        now: Optional[datetime] = None,
    ) -> Optional[datetime]:
        now = ensure_utc(now or utc_now())
        slots = self.generate_slots(
            professional_id,
            start_at=now,
            end_at=now + timedelta(days=30),
            consultation_type=consultation_type,
        )
        return slots[0].start_at if slots else None

    def upsert_presence(
        self,
        professional_id: uuid.UUID,
        *,
        manual_status_override: Optional[str],
        accepting_consultations: Optional[bool],
        heartbeat: bool,
    ) -> Tuple[ProfessionalPresence, str, str]:
        presence = (
            self.session.query(ProfessionalPresence)
            .filter(ProfessionalPresence.professional_id == professional_id)
            .first()
        )
        if not presence:
            presence = ProfessionalPresence(professional_id=professional_id)
            self.session.add(presence)

        if manual_status_override is not None:
            if manual_status_override not in {"away", "offline", ""}:
                raise ValueError("manual_status_override must be away, offline, or empty")
            presence.manual_status_override = manual_status_override or None
        if accepting_consultations is not None:
            presence.accepting_consultations = accepting_consultations
        if heartbeat:
            presence.last_heartbeat_at = utc_now()
        presence.updated_at = utc_now()
        self.session.flush()

        status, label = effective_presence_status(presence)
        return presence, status, label

    def _candidate_windows(
        self,
        professional_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
        consultation_type: str,
    ) -> List[Tuple[datetime, datetime, str]]:
        windows: List[Tuple[datetime, datetime, str]] = []
        rules = (
            self.session.query(ProfessionalAvailabilityRule)
            .filter(
                ProfessionalAvailabilityRule.professional_id == professional_id,
                ProfessionalAvailabilityRule.is_active.is_(True),
            )
            .all()
        )
        exceptions = (
            self.session.query(ProfessionalAvailabilityException)
            .filter(
                ProfessionalAvailabilityException.professional_id == professional_id,
                ProfessionalAvailabilityException.is_available.is_(True),
                ProfessionalAvailabilityException.end_at > start_at,
                ProfessionalAvailabilityException.start_at < end_at,
            )
            .all()
        )

        for rule in rules:
            if not self._supports_consultation_type(rule.consultation_types, consultation_type):
                continue
            tz = parse_timezone(rule.timezone)
            local_start = start_at.astimezone(tz).date() - timedelta(days=1)
            local_end = end_at.astimezone(tz).date() + timedelta(days=1)
            day = local_start
            while day <= local_end:
                if day.weekday() == rule.weekday:
                    ws = self._local_day_minute(day, rule.start_minute, tz)
                    we = self._local_day_minute(day, rule.end_minute, tz)
                    if we > start_at and ws < end_at and we > ws:
                        windows.append((ws, we, rule.timezone))
                day += timedelta(days=1)

        for exc in exceptions:
            if self._supports_consultation_type(exc.consultation_types, consultation_type):
                windows.append((ensure_utc(exc.start_at), ensure_utc(exc.end_at), exc.timezone))

        return windows

    def _blocking_windows(
        self,
        professional_id: uuid.UUID,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> List[Tuple[datetime, datetime]]:
        blockers: List[Tuple[datetime, datetime]] = []
        unavailable_exceptions = (
            self.session.query(ProfessionalAvailabilityException)
            .filter(
                ProfessionalAvailabilityException.professional_id == professional_id,
                ProfessionalAvailabilityException.is_available.is_(False),
                ProfessionalAvailabilityException.end_at > start_at,
                ProfessionalAvailabilityException.start_at < end_at,
            )
            .all()
        )
        for exc in unavailable_exceptions:
            blockers.append((ensure_utc(exc.start_at), ensure_utc(exc.end_at)))

        bookings = (
            self.session.query(ConsultationBookingRequest)
            .filter(
                ConsultationBookingRequest.professional_id == professional_id,
                ConsultationBookingRequest.status.in_(BOOKING_CONFLICT_STATUSES),
                ConsultationBookingRequest.requested_end_at > start_at,
                ConsultationBookingRequest.requested_start_at < end_at,
            )
            .all()
        )
        for booking in bookings:
            blockers.append((ensure_utc(booking.requested_start_at), ensure_utc(booking.requested_end_at)))

        return blockers

    def _build_rule(self, professional_id: uuid.UUID, raw: Dict[str, Any]) -> ProfessionalAvailabilityRule:
        weekday = int(raw.get("weekday"))
        start_minute = int(raw.get("start_minute"))
        end_minute = int(raw.get("end_minute"))
        timezone_name = raw.get("timezone") or "Europe/London"
        parse_timezone(timezone_name)
        if weekday < 0 or weekday > 6:
            raise ValueError("weekday must be between 0 and 6")
        if start_minute < 0 or start_minute >= 24 * 60:
            raise ValueError("start_minute must be between 0 and 1439")
        if end_minute <= start_minute or end_minute > 24 * 60:
            raise ValueError("end_minute must be after start_minute and no more than 1440")
        return ProfessionalAvailabilityRule(
            professional_id=professional_id,
            weekday=weekday,
            start_minute=start_minute,
            end_minute=end_minute,
            timezone=timezone_name,
            consultation_types=raw.get("consultation_types"),
            is_active=bool(raw.get("is_active", True)),
        )

    def _build_exception(self, professional_id: uuid.UUID, raw: Dict[str, Any]) -> ProfessionalAvailabilityException:
        start_at = parse_datetime(raw.get("start_at"), field_name="start_at")
        end_at = parse_datetime(raw.get("end_at"), field_name="end_at")
        timezone_name = raw.get("timezone") or "Europe/London"
        parse_timezone(timezone_name)
        if end_at <= start_at:
            raise ValueError("exception end_at must be after start_at")
        return ProfessionalAvailabilityException(
            professional_id=professional_id,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone_name,
            is_available=bool(raw.get("is_available", False)),
            consultation_types=raw.get("consultation_types"),
            reason=raw.get("reason"),
        )

    @staticmethod
    def _rule_to_dict(rule: ProfessionalAvailabilityRule) -> Dict[str, Any]:
        return {
            "id": str(rule.id),
            "weekday": rule.weekday,
            "start_minute": rule.start_minute,
            "end_minute": rule.end_minute,
            "timezone": rule.timezone,
            "consultation_types": rule.consultation_types or [],
            "is_active": rule.is_active,
        }

    @staticmethod
    def _exception_to_dict(exc: ProfessionalAvailabilityException) -> Dict[str, Any]:
        return {
            "id": str(exc.id),
            "start_at": ensure_utc(exc.start_at).isoformat(),
            "end_at": ensure_utc(exc.end_at).isoformat(),
            "timezone": exc.timezone,
            "is_available": exc.is_available,
            "consultation_types": exc.consultation_types or [],
            "reason": exc.reason,
        }

    @staticmethod
    def _supports_consultation_type(supported: Optional[Iterable[str]], consultation_type: str) -> bool:
        return not supported or consultation_type in supported

    @staticmethod
    def _local_day_minute(local_date: date, minute: int, tz: ZoneInfo) -> datetime:
        local_dt = datetime.combine(local_date, time(hour=minute // 60, minute=minute % 60), tzinfo=tz)
        return local_dt.astimezone(timezone.utc)

    @staticmethod
    def _overlaps_any(start_at: datetime, end_at: datetime, blockers: Iterable[Tuple[datetime, datetime]]) -> bool:
        return any(block_start < end_at and start_at < block_end for block_start, block_end in blockers)
