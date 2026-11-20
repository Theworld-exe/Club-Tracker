"""Pydantic v2 schemas and canonical normalization helpers for production Club Operations."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

CANONICAL_DAYS: tuple[str, ...] = (
    "Saturday",
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
)

CANONICAL_TIME_SLOTS: tuple[str, ...] = (
    "08:00-09:30",
    "09:30-11:00",
    "11:00-12:30",
    "12:30-14:00",
    "14:00-15:30",
    "15:30-17:00",
)

SESSION_TYPES: tuple[str, ...] = ("COURS", "TD", "TP")

DEFAULT_DEPARTMENTS: tuple[str, ...] = (
    "Dev",
    "Design",
    "Media",
    "HR",
    "Logistics",
)

DEFAULT_ROLES: tuple[str, ...] = (
    "Member",
    "Trainee",
    "Senior Member",
    "Dept Lead",
    "Bureau",
    "Vice President",
    "President",
)

CanonicalDay = Literal[
    "Saturday",
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
]

CanonicalTimeSlot = Literal[
    "08:00-09:30",
    "09:30-11:00",
    "11:00-12:30",
    "12:30-14:00",
    "14:00-15:30",
    "15:30-17:00",
]

SessionType = Literal["COURS", "TD", "TP"]
MemberStatusType = Literal["FREE", "COURS", "TD", "TP"]

_DAY_ALIASES: dict[str, str] = {
    "saturday": "Saturday",
    "sat": "Saturday",
    "samedi": "Saturday",
    "sam": "Saturday",
    "سبت": "Saturday",
    "السبت": "Saturday",
    "sunday": "Sunday",
    "sun": "Sunday",
    "dimanche": "Sunday",
    "dim": "Sunday",
    "أحد": "Sunday",
    "الأحد": "Sunday",
    "monday": "Monday",
    "mon": "Monday",
    "lundi": "Monday",
    "lun": "Monday",
    "إثنين": "Monday",
    "الاثنين": "Monday",
    "tuesday": "Tuesday",
    "tue": "Tuesday",
    "tues": "Tuesday",
    "mardi": "Tuesday",
    "mar": "Tuesday",
    "ثلاثاء": "Tuesday",
    "الثلاثاء": "Tuesday",
    "wednesday": "Wednesday",
    "wed": "Wednesday",
    "mercredi": "Wednesday",
    "mer": "Wednesday",
    "أربعاء": "Wednesday",
    "الأربعاء": "Wednesday",
    "thursday": "Thursday",
    "thu": "Thursday",
    "thur": "Thursday",
    "thurs": "Thursday",
    "jeudi": "Thursday",
    "jeu": "Thursday",
    "خميس": "Thursday",
    "الخميس": "Thursday",
}

_CANONICAL_SLOT_MINUTES: list[tuple[str, int, int]] = [
    ("08:00-09:30", 8 * 60, 9 * 60 + 30),
    ("09:30-11:00", 9 * 60 + 30, 11 * 60),
    ("11:00-12:30", 11 * 60, 12 * 60 + 30),
    ("12:30-14:00", 12 * 60 + 30, 14 * 60),
    ("14:00-15:30", 14 * 60, 15 * 60 + 30),
    ("15:30-17:00", 15 * 60 + 30, 17 * 60),
]

_DEPT_CANONICAL_MAP: dict[str, str] = {d.lower(): d for d in DEFAULT_DEPARTMENTS}
_ROLE_CANONICAL_MAP: dict[str, str] = {r.lower(): r for r in DEFAULT_ROLES}


def _clean_optional_str(v: object) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v).strip())
    return s if s else None


def normalize_day(value: str) -> str:
    """Normalize English/French/Arabic/abbreviated day names to canonical Saturday..Thursday."""
    if not isinstance(value, str):
        raise ValueError("Day must be a string.")
    cleaned = value.strip().lower().rstrip(".")
    if cleaned in _DAY_ALIASES:
        return _DAY_ALIASES[cleaned]
    raise ValueError(
        f"Invalid day '{value}'. Must be one of: {', '.join(CANONICAL_DAYS)}."
    )


def normalize_time_slot(value: str, snap_nearest: bool = False) -> str:
    """Normalize a time slot string into one of the 6 canonical 90-min blocks."""
    if not isinstance(value, str):
        raise ValueError("Time slot must be a string.")
    raw = value.strip()
    if raw in CANONICAL_TIME_SLOTS:
        return raw

    normalized = raw.lower()
    normalized = re.sub(r"\s*(?:–|—|to|à|/)\s*", "-", normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(
        r"(\d{1,2})\s*[h.]\s*(\d{0,2})",
        lambda m: f"{int(m.group(1)):02d}:{int(m.group(2) or '0'):02d}",
        normalized,
    )

    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?", normalized)
    if match:
        sh, sm, eh, em = (
            int(match.group(1)),
            int(match.group(2) or 0),
            int(match.group(3)),
            int(match.group(4) or 0),
        )
        formatted = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
        if formatted in CANONICAL_TIME_SLOTS:
            return formatted

        if snap_nearest:
            start_min = sh * 60 + sm
            end_min = eh * 60 + em
            mid = (start_min + end_min) / 2.0
            best_slot = min(
                _CANONICAL_SLOT_MINUTES,
                key=lambda item: abs(((item[1] + item[2]) / 2.0) - mid),
            )
            if 7 * 60 <= start_min <= 18 * 60 + 30:
                return best_slot[0]

    raise ValueError(
        f"Invalid time_slot '{value}'. Expected one of: {', '.join(CANONICAL_TIME_SLOTS)}."
    )


def normalize_group_code(value: str, allow_all: bool = False) -> str:
    """Normalize group strings so '15', 'g15', '015', 'Groupe 15', and 'G15' -> 'G15'."""
    if not isinstance(value, str):
        raise ValueError("Group must be a string.")
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("Group cannot be empty.")

    if cleaned in {"ALL", "TOUS", "TOUTE LA SECTION", "SECTION", "PROMO", "*"}:
        if allow_all:
            return "ALL"
        raise ValueError("Member group_name must be a specific group (e.g., 'G1'), not 'ALL'.")

    cleaned = re.sub(r"^(?:GROUPE|GROUP|GRP)[\s.\-_:]*", "", cleaned).strip()
    cleaned = re.sub(r"^G[\s.\-_:]+", "G", cleaned)

    num_match = re.fullmatch(r"G?0*(\d+)", cleaned)
    if num_match:
        return f"G{int(num_match.group(1))}"

    alnum_match = re.fullmatch(r"G?0*(\d+[A-Z]+)", cleaned)
    if alnum_match:
        return f"G{alnum_match.group(1)}"

    if re.fullmatch(r"[A-Z0-9_]+", cleaned):
        return cleaned if cleaned.startswith("G") else f"G{cleaned}"

    raise ValueError(f"Invalid group identifier '{value}'. Example valid values: 'G1', 'G15'.")


def normalize_session_type(value: str) -> SessionType:
    """Normalize session type strictly to COURS, TD, or TP."""
    if not isinstance(value, str):
        raise ValueError("session_type must be a string.")
    cleaned = value.strip().upper()
    if cleaned in {"COURS", "COUR", "CM", "LECTURE", "CONF", "AMPHI"}:
        return "COURS"
    if cleaned in {"TD", "TRAVAUX DIRIGES", "TRAVAUX DIRIGÉS"}:
        return "TD"
    if cleaned in {"TP", "TRAVAUX PRATIQUES", "LAB", "LABO"}:
        return "TP"
    raise ValueError(
        f"Invalid session_type '{value}'. Must be strictly 'COURS', 'TD', or 'TP'."
    )


def normalize_department(value: str) -> str:
    """Normalize standard club department names while preserving custom ones."""
    if not isinstance(value, str):
        raise ValueError("Department must be a string.")
    cleaned = re.sub(r"\s+", " ", value.strip())
    if not cleaned:
        raise ValueError("Department cannot be empty.")
    return _DEPT_CANONICAL_MAP.get(cleaned.lower(), cleaned)


def normalize_role(value: str | None) -> str:
    """Normalize member role title, defaulting to 'Member'."""
    if value is None:
        return "Member"
    cleaned = re.sub(r"\s+", " ", str(value).strip())
    if not cleaned:
        return "Member"
    return _ROLE_CANONICAL_MAP.get(cleaned.lower(), cleaned)


# ---------------------------------------------------------------------------
# Schedule Slot Schemas
# ---------------------------------------------------------------------------


class ScheduleSlotCreate(BaseModel):
    day: str
    time_slot: str
    group_target: str = "ALL"
    session_type: SessionType
    module_name: str | None = None
    room: str | None = None

    @field_validator("day", mode="before")
    @classmethod
    def _val_day(cls, v: str) -> str:
        return normalize_day(v)

    @field_validator("time_slot", mode="before")
    @classmethod
    def _val_time_slot(cls, v: str) -> str:
        return normalize_time_slot(v, snap_nearest=False)

    @field_validator("session_type", mode="before")
    @classmethod
    def _val_session_type(cls, v: str) -> SessionType:
        return normalize_session_type(v)

    @field_validator("group_target", mode="before")
    @classmethod
    def _val_group_target(cls, v: str) -> str:
        return normalize_group_code(v, allow_all=True)

    @field_validator("module_name", "room", mode="before")
    @classmethod
    def _val_optional_text(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class ScheduleSlotUpdate(BaseModel):
    day: str | None = None
    time_slot: str | None = None
    group_target: str | None = None
    session_type: SessionType | None = None
    module_name: str | None = None
    room: str | None = None

    @field_validator("day", mode="before")
    @classmethod
    def _val_day(cls, v: str | None) -> str | None:
        return None if v is None else normalize_day(v)

    @field_validator("time_slot", mode="before")
    @classmethod
    def _val_time_slot(cls, v: str | None) -> str | None:
        return None if v is None else normalize_time_slot(v, snap_nearest=False)

    @field_validator("session_type", mode="before")
    @classmethod
    def _val_session_type(cls, v: str | None) -> SessionType | None:
        return None if v is None else normalize_session_type(v)

    @field_validator("group_target", mode="before")
    @classmethod
    def _val_group_target(cls, v: str | None) -> str | None:
        return None if v is None else normalize_group_code(v, allow_all=True)

    @field_validator("module_name", "room", mode="before")
    @classmethod
    def _val_optional_text(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class ScheduleSlotRead(BaseModel):
    id: int
    promo_id: int
    day: str
    time_slot: str
    group_target: str
    session_type: SessionType
    module_name: str | None = None
    room: str | None = None


# ---------------------------------------------------------------------------
# Promo Schemas
# ---------------------------------------------------------------------------


class PromoCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(validation_alias=AliasChoices("name", "promo_name"))
    academic_year: str | None = None
    schedule_slots: list[ScheduleSlotCreate] = Field(
        default_factory=list,
        validation_alias=AliasChoices("schedule_slots", "slots"),
    )
    overwrite_if_exists: bool = False
    merge_slots: bool = False

    @field_validator("name", mode="before")
    @classmethod
    def _val_name(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Promo name cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("academic_year", mode="before")
    @classmethod
    def _val_year(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class PromoUpdate(BaseModel):
    name: str | None = None
    academic_year: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _val_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Promo name cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("academic_year", mode="before")
    @classmethod
    def _val_year(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class PromoRead(BaseModel):
    id: int
    name: str
    academic_year: str | None = None
    created_at: str
    slot_count: int = 0
    member_count: int = 0
    groups: list[str] = Field(default_factory=list)
    slot_groups: list[str] = Field(default_factory=list)


class PromoWithSlotsRead(PromoRead):
    schedule_slots: list[ScheduleSlotRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Rich Member Schemas
# ---------------------------------------------------------------------------


class MemberCreate(BaseModel):
    full_name: str
    department: str
    role: str = "Member"
    promo_id: int
    group_name: str
    discord: str | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool = True
    shift_exempt: bool = False
    notes: str | None = None
    allow_duplicate: bool = False

    @field_validator("full_name", mode="before")
    @classmethod
    def _val_name(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Member full_name cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("department", mode="before")
    @classmethod
    def _val_dept(cls, v: str) -> str:
        return normalize_department(v)

    @field_validator("role", mode="before")
    @classmethod
    def _val_role(cls, v: str | None) -> str:
        return normalize_role(v)

    @field_validator("group_name", mode="before")
    @classmethod
    def _val_group(cls, v: str) -> str:
        return normalize_group_code(v, allow_all=False)

    @field_validator("discord", "phone", "email", "notes", mode="before")
    @classmethod
    def _val_opt(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class MemberUpdate(BaseModel):
    full_name: str | None = None
    department: str | None = None
    role: str | None = None
    promo_id: int | None = None
    group_name: str | None = None
    discord: str | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool | None = None
    shift_exempt: bool | None = None
    notes: str | None = None

    @field_validator("full_name", mode="before")
    @classmethod
    def _val_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Member full_name cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("department", mode="before")
    @classmethod
    def _val_dept(cls, v: str | None) -> str | None:
        return None if v is None else normalize_department(v)

    @field_validator("role", mode="before")
    @classmethod
    def _val_role(cls, v: str | None) -> str | None:
        return None if v is None else normalize_role(v)

    @field_validator("group_name", mode="before")
    @classmethod
    def _val_group(cls, v: str | None) -> str | None:
        return None if v is None else normalize_group_code(v, allow_all=False)

    @field_validator("discord", "phone", "email", "notes", mode="before")
    @classmethod
    def _val_opt(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class MemberRead(BaseModel):
    id: int
    full_name: str
    department: str
    role: str = "Member"
    promo_id: int
    promo_name: str
    group_name: str
    discord: str | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool = True
    shift_exempt: bool = False
    notes: str | None = None
    created_at: str | None = None
    matching_slots_count: int | None = None
    schedule_status: str | None = None


class PromoAssignMembersPayload(BaseModel):
    member_ids: list[int] = Field(min_length=1)
    group_name: str | None = None

    @field_validator("group_name", mode="before")
    @classmethod
    def _val_group(cls, v: object) -> str | None:
        if v is None or not str(v).strip():
            return None
        return normalize_group_code(str(v), allow_all=False)


class PromoMemberQuickCreate(BaseModel):
    full_name: str
    department: str
    role: str = "Member"
    group_name: str
    discord: str | None = None
    phone: str | None = None
    email: str | None = None
    is_active: bool = True
    shift_exempt: bool = False
    notes: str | None = None

    @field_validator("full_name", mode="before")
    @classmethod
    def _val_name(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Member full_name cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("department", mode="before")
    @classmethod
    def _val_dept(cls, v: str) -> str:
        return normalize_department(v)

    @field_validator("role", mode="before")
    @classmethod
    def _val_role(cls, v: str | None) -> str:
        return normalize_role(v)

    @field_validator("group_name", mode="before")
    @classmethod
    def _val_group(cls, v: str) -> str:
        return normalize_group_code(v, allow_all=False)

    @field_validator("discord", "phone", "email", "notes", mode="before")
    @classmethod
    def _val_opt(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class BulkAssignResult(BaseModel):
    promo_id: int
    promo_name: str
    assigned_count: int
    members: list[MemberRead]


class BulkCSVImportResult(BaseModel):
    imported_count: int
    updated_count: int = 0
    created_promos: list[str] = Field(default_factory=list)
    skipped_count: int = 0
    errors: list[str] = Field(default_factory=list)
    members: list[MemberRead] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AI Vision Structured Extraction Schemas (Single & Multi-Section PDFs)
# ---------------------------------------------------------------------------


class ExtractedSlot(BaseModel):
    """Single normalized schedule slot extracted from a university timetable."""

    day: CanonicalDay = Field(
        description="Day of the week: Saturday, Sunday, Monday, Tuesday, Wednesday, or Thursday."
    )
    time_slot: CanonicalTimeSlot = Field(
        description=(
            "Normalized 90-minute canonical time block: "
            "'08:00-09:30', '09:30-11:00', '11:00-12:30', "
            "'12:30-14:00', '14:00-15:30', or '15:30-17:00'."
        )
    )
    group_target: str = Field(
        description=(
            "'ALL' for section-wide lectures (COURS), or a single normalized uppercase "
            "group code such as 'G1', 'G2', 'G3' for TD/TP. Never combine multiple groups "
            "in one string; split multi-group cells into separate ExtractedSlot items."
        )
    )
    session_type: SessionType = Field(
        description="Strictly 'COURS', 'TD', or 'TP'."
    )
    module_name: str | None = Field(
        default=None,
        description="Course or module short name, e.g. 'Algorithmique', 'Archi', 'OS', 'Analyse 2'.",
    )
    room: str | None = Field(
        default=None,
        description="Classroom, amphitheater, or lab room, e.g. 'Amphi A', 'Salle 12', 'Labo 3'.",
    )


class TimetableExtraction(BaseModel):
    """Structured timetable extraction for a single academic promo/section."""

    promo_name: str = Field(
        description="Academic promo and section title, e.g. 'L1 Info Sec 2', 'L2 Info SI', 'M1 IA'."
    )
    slots: list[ExtractedSlot] = Field(
        default_factory=list,
        description="List of all extracted class sessions mapped to canonical slots and individual groups.",
    )


class MultiTimetableExtraction(BaseModel):
    """Container supporting PDFs that contain one OR multiple promo/section timetables across pages."""

    timetables: list[TimetableExtraction] = Field(
        description="All promo/section timetables found in the uploaded document (1 item for a single timetable, or multiple items if the PDF contains multiple sections/pages)."
    )


class ScanStagingResponse(BaseModel):
    """Response returned by POST /api/promos/scan."""

    promo_name: str
    slots: list[ExtractedSlot] = Field(default_factory=list)
    timetables: list[TimetableExtraction] = Field(default_factory=list)
    source_filename: str | None = None


# ---------------------------------------------------------------------------
# Availability, Heatmap & Shift Schemas
# ---------------------------------------------------------------------------


class MemberSlotItem(BaseModel):
    id: int
    full_name: str
    department: str
    role: str = "Member"
    promo_id: int
    promo_name: str
    group_name: str
    discord: str | None = None
    phone: str | None = None
    is_active: bool = True
    shift_exempt: bool = False
    status: MemberStatusType
    weight: float
    module_name: str | None = None
    room: str | None = None
    overlap_note: str | None = None


class SlotBreakdownResponse(BaseModel):
    day: CanonicalDay
    time_slot: CanonicalTimeSlot
    department: str | None = None
    promo_id: int | None = None
    total_members: int
    counts: dict[str, int]
    strict_free_count: int
    flexible_free_count: int
    strict_percentage: float
    weighted_score: float
    FREE: list[MemberSlotItem] = Field(default_factory=list)
    COURS: list[MemberSlotItem] = Field(default_factory=list)
    TD: list[MemberSlotItem] = Field(default_factory=list)
    TP: list[MemberSlotItem] = Field(default_factory=list)


class HeatmapCell(BaseModel):
    day: CanonicalDay
    time_slot: CanonicalTimeSlot
    total_members: int
    strict_free_count: int
    cours_count: int
    td_count: int
    tp_count: int
    flexible_free_count: int
    strict_percentage: float
    weighted_points: float
    weighted_score: float


class HeatmapResponse(BaseModel):
    department: str | None = None
    promo_id: int | None = None
    total_members: int
    days: list[str]
    time_slots: list[str]
    cells: list[HeatmapCell]
    top_recommended_slots: list[HeatmapCell]
    top_strict_slots: list[HeatmapCell]


class ShiftRequest(BaseModel):
    day: str
    time_slots: list[str] = Field(min_length=1)
    members_per_shift: int = Field(default=3, ge=1, le=30)
    department: str | None = None
    promo_id: int | None = None
    allow_cours_fallback: bool = True
    include_shift_exempt: bool = False
    excluded_member_ids: list[int] = Field(default_factory=list)

    @field_validator("day", mode="before")
    @classmethod
    def _val_day(cls, v: str) -> str:
        return normalize_day(v)

    @field_validator("time_slots", mode="before")
    @classmethod
    def _val_slots(cls, v: list[str]) -> list[str]:
        if not isinstance(v, list) or not v:
            raise ValueError("At least one time_slot must be selected.")
        normalized = [normalize_time_slot(s) for s in v]
        slot_order = {s: idx for idx, s in enumerate(CANONICAL_TIME_SLOTS)}
        return sorted(set(normalized), key=lambda s: slot_order[s])


class AssignedShiftMember(BaseModel):
    id: int
    full_name: str
    department: str
    role: str = "Member"
    promo_id: int
    promo_name: str
    group_name: str
    discord: str | None = None
    phone: str | None = None
    availability_status: Literal["FREE", "COURS"]
    status_note: str
    module_name: str | None = None
    room: str | None = None
    shifts_assigned_today: int


class ShiftSlotAssignment(BaseModel):
    time_slot: CanonicalTimeSlot
    requested_count: int
    filled_count: int
    shortfall: int
    free_pool_count: int
    cours_pool_count: int
    assigned_members: list[AssignedShiftMember] = Field(default_factory=list)


class MemberShiftSummary(BaseModel):
    id: int
    full_name: str
    department: str
    role: str = "Member"
    promo_name: str
    group_name: str
    discord: str | None = None
    phone: str | None = None
    total_shifts: int
    free_shifts: int
    cours_shifts: int
    assigned_slots: list[str]


class ShiftScheduleResponse(BaseModel):
    day: CanonicalDay
    department: str | None = None
    promo_id: int | None = None
    members_per_shift: int
    allow_cours_fallback: bool
    excluded_exempt_count: int = 0
    shifts: list[ShiftSlotAssignment]
    member_summaries: list[MemberShiftSummary]
    unfilled_spots_total: int


# ---------------------------------------------------------------------------
# Saved Shift Plans & Saved Meetings Schemas
# ---------------------------------------------------------------------------


class SavedShiftPlanCreate(BaseModel):
    title: str
    event_date: str | None = None
    notes: str | None = None
    schedule: ShiftScheduleResponse

    @field_validator("title", mode="before")
    @classmethod
    def _val_title(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Shift plan title cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("event_date", "notes", mode="before")
    @classmethod
    def _val_opt(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class SavedShiftPlanUpdate(BaseModel):
    title: str | None = None
    event_date: str | None = None
    notes: str | None = None
    schedule: ShiftScheduleResponse | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _val_title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Shift plan title cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("event_date", "notes", mode="before")
    @classmethod
    def _val_opt(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class SavedShiftPlanRead(BaseModel):
    id: int
    title: str
    event_date: str | None = None
    day: str
    department: str | None = None
    promo_id: int | None = None
    members_per_shift: int
    allow_cours_fallback: bool
    notes: str | None = None
    schedule: ShiftScheduleResponse
    created_at: str
    updated_at: str


class SavedMeetingCreate(BaseModel):
    title: str
    meeting_date: str | None = None
    day: str
    time_slot: str
    department: str | None = None
    promo_id: int | None = None
    location: str | None = None
    notes: str | None = None

    @field_validator("title", mode="before")
    @classmethod
    def _val_title(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Meeting title cannot be empty.")
        return re.sub(r"\s+", " ", v.strip())

    @field_validator("day", mode="before")
    @classmethod
    def _val_day(cls, v: str) -> str:
        return normalize_day(v)

    @field_validator("time_slot", mode="before")
    @classmethod
    def _val_slot(cls, v: str) -> str:
        return normalize_time_slot(v)

    @field_validator("meeting_date", "location", "notes", mode="before")
    @classmethod
    def _val_opt(cls, v: object) -> str | None:
        return _clean_optional_str(v)


class SavedMeetingRead(BaseModel):
    id: int
    title: str
    meeting_date: str | None = None
    day: str
    time_slot: str
    department: str | None = None
    promo_id: int | None = None
    location: str | None = None
    notes: str | None = None
    expected_free_count: int
    expected_cours_count: int
    total_cohort_count: int
    weighted_score: float
    created_at: str
