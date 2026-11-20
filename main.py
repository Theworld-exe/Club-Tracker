"""Production FastAPI server for the University Club Timetable & Availability Analyzer."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anyio
from dotenv import load_dotenv, set_key
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from database import (
    APP_DIR,
    BASE_DIR,  # noqa: F401 - re-exported for compatibility
    BUNDLE_DIR,
    export_sqlite_backup_bytes,
    get_db,
    init_db,
)
from engine import (
    generate_exhibition_shifts,
    generate_weekly_heatmap,
    get_slot_breakdown,
)
from models import (
    CANONICAL_DAYS,
    CANONICAL_TIME_SLOTS,
    BulkAssignResult,
    BulkCSVImportResult,
    HeatmapResponse,
    MemberCreate,
    MemberRead,
    MemberUpdate,
    PromoAssignMembersPayload,
    PromoCreate,
    PromoMemberQuickCreate,
    PromoRead,
    PromoUpdate,
    PromoWithSlotsRead,
    SavedMeetingCreate,
    SavedMeetingRead,
    SavedShiftPlanCreate,
    SavedShiftPlanRead,
    SavedShiftPlanUpdate,
    ScanStagingResponse,
    ScheduleSlotCreate,
    ScheduleSlotRead,
    ScheduleSlotUpdate,
    ShiftRequest,
    ShiftScheduleResponse,
    SlotBreakdownResponse,
    normalize_day,
    normalize_department,
    normalize_group_code,
    normalize_role,
    normalize_session_type,
    normalize_time_slot,
)
from parser import get_active_gemini_model, parse_timetable_bytes, test_gemini_api_key

ENV_FILE = Path(os.getenv("CLUB_ENV_PATH", str(APP_DIR / ".env")))
STATIC_DIR = BUNDLE_DIR / "static" if (BUNDLE_DIR / "static").exists() else APP_DIR / "static"

MAX_TIMETABLE_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_IMPORT_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

load_dotenv(ENV_FILE)


def _redact_secret(message: str, secret: str) -> str:
    """Prevent accidental API key exposure in returned error messages."""
    if secret and secret in message:
        return message.replace(secret, "[REDACTED]")
    return message


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_dotenv(ENV_FILE)
    init_db()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="University Club Timetable & Availability Analyzer",
    description=(
        "Local-first timetable scanner, availability heatmap engine, "
        "meeting scheduler, and stand shift planner for university clubs."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------

_DAY_ORDER = {day: i for i, day in enumerate(CANONICAL_DAYS)}
_SLOT_ORDER = {slot: i for i, slot in enumerate(CANONICAL_TIME_SLOTS)}
_TYPE_ORDER = {"COURS": 0, "TD": 1, "TP": 2}


def _parse_bool_cell(val: str | None, default: bool) -> bool:
    if val is None or not str(val).strip():
        return default
    cleaned = str(val).strip().lower()
    if cleaned in {"1", "true", "yes", "y", "oui", "active", "on"}:
        return True
    if cleaned in {"0", "false", "no", "n", "non", "inactive", "off"}:
        return False
    return default


def _natural_group_key(group_code: str) -> tuple[int, int, str]:
    if group_code == "ALL":
        return (0, 0, "")
    m = re.match(r"^G(\d+)(.*)$", group_code)
    if m:
        return (1, int(m.group(1)), m.group(2))
    return (2, 0, group_code)


def _sort_slot_rows(rows: list[sqlite3.Row]) -> list[ScheduleSlotRead]:
    items = [ScheduleSlotRead(**dict(r)) for r in rows]
    items.sort(
        key=lambda s: (
            _DAY_ORDER.get(s.day, 99),
            _SLOT_ORDER.get(s.time_slot, 99),
            _TYPE_ORDER.get(s.session_type, 99),
            _natural_group_key(s.group_target),
        )
    )
    return items


def _build_promo_read(conn: sqlite3.Connection, promo_row: sqlite3.Row) -> PromoRead:
    promo_id = int(promo_row["id"])
    slot_count = conn.execute(
        "SELECT COUNT(*) FROM schedule_slots WHERE promo_id = ?", (promo_id,)
    ).fetchone()[0]
    member_count = conn.execute(
        "SELECT COUNT(*) FROM members WHERE promo_id = ?", (promo_id,)
    ).fetchone()[0]

    slot_groups = conn.execute(
        "SELECT DISTINCT group_target FROM schedule_slots WHERE promo_id = ? AND group_target != 'ALL'",
        (promo_id,),
    ).fetchall()
    member_groups = conn.execute(
        "SELECT DISTINCT group_name FROM members WHERE promo_id = ?",
        (promo_id,),
    ).fetchall()
    distinct_slot_groups = sorted(
        {r[0] for r in slot_groups},
        key=_natural_group_key,
    )
    distinct_groups = sorted(
        {r[0] for r in slot_groups} | {r[0] for r in member_groups},
        key=_natural_group_key,
    )
    return PromoRead(
        id=promo_id,
        name=promo_row["name"],
        academic_year=promo_row["academic_year"],
        created_at=str(promo_row["created_at"]),
        slot_count=slot_count,
        member_count=member_count,
        groups=distinct_groups,
        slot_groups=distinct_slot_groups,
    )


def _row_to_member_read(row: sqlite3.Row) -> MemberRead:
    data = dict(row)
    data["is_active"] = bool(data.get("is_active", 1))
    data["shift_exempt"] = bool(data.get("shift_exempt", 0))
    data["role"] = data.get("role") or "Member"
    if data.get("created_at") is not None:
        data["created_at"] = str(data["created_at"])
    return MemberRead(**data)


def _build_annotated_members(
    conn: sqlite3.Connection, rows: list[sqlite3.Row]
) -> list[MemberRead]:
    """Annotate members with matching timetable slot count and live schedule linkage status."""
    if not rows:
        return []

    promo_ids = sorted({int(r["promo_id"]) for r in rows if r["promo_id"]})
    stats_map: dict[tuple[int, str], int] = defaultdict(int)

    if promo_ids:
        placeholders = ",".join("?" for _ in promo_ids)
        slot_summary = conn.execute(
            f"""
            SELECT promo_id, group_target, COUNT(*) as cnt
            FROM schedule_slots
            WHERE promo_id IN ({placeholders})
            GROUP BY promo_id, group_target
            """,
            promo_ids,
        ).fetchall()
        for s in slot_summary:
            stats_map[(int(s["promo_id"]), s["group_target"])] = int(s["cnt"])

    annotated: list[MemberRead] = []
    for r in rows:
        m = _row_to_member_read(r)
        pid = m.promo_id
        grp = m.group_name

        cours_cnt = stats_map.get((pid, "ALL"), 0)
        group_cnt = stats_map.get((pid, grp), 0)
        total_matching = cours_cnt + group_cnt

        m.matching_slots_count = total_matching
        if group_cnt > 0:
            m.schedule_status = f"{total_matching} slots ({group_cnt} {grp} + {cours_cnt} Cours)"
        elif cours_cnt > 0:
            m.schedule_status = f"{cours_cnt} Cours (no {grp} TD/TP slots)"
        else:
            m.schedule_status = "No timetable slots found"

        annotated.append(m)

    return annotated


def _fetch_member_read(conn: sqlite3.Connection, member_id: int) -> MemberRead:
    row = conn.execute(
        """
        SELECT
            m.id, m.full_name, m.department, m.role, m.promo_id,
            p.name AS promo_name, m.group_name, m.discord, m.phone,
            m.email, m.is_active, m.shift_exempt, m.notes, m.created_at
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        WHERE m.id = ?
        """,
        (member_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    return _build_annotated_members(conn, [row])[0]


def _row_to_shift_plan_read(row: sqlite3.Row) -> SavedShiftPlanRead:
    sched_dict = json.loads(row["schedule_json"])
    return SavedShiftPlanRead(
        id=int(row["id"]),
        title=row["title"],
        event_date=row["event_date"],
        day=row["day"],
        department=row["department"],
        promo_id=row["promo_id"],
        members_per_shift=int(row["members_per_shift"]),
        allow_cours_fallback=bool(row["allow_cours_fallback"]),
        notes=row["notes"],
        schedule=ShiftScheduleResponse.model_validate(sched_dict),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _build_live_meeting_read(conn: sqlite3.Connection, row: sqlite3.Row) -> SavedMeetingRead:
    """Compute live expected attendance against current promos/members for a saved meeting."""
    bd = get_slot_breakdown(
        conn=conn,
        day=row["day"],
        time_slot=row["time_slot"],
        department=row["department"],
        promo_id=row["promo_id"],
        include_inactive=False,
    )
    return SavedMeetingRead(
        id=int(row["id"]),
        title=row["title"],
        meeting_date=row["meeting_date"],
        day=row["day"],
        time_slot=row["time_slot"],
        department=row["department"],
        promo_id=row["promo_id"],
        location=row["location"],
        notes=row["notes"],
        expected_free_count=bd.strict_free_count,
        expected_cours_count=bd.counts.get("COURS", 0),
        total_cohort_count=bd.total_members,
        weighted_score=bd.weighted_score,
        created_at=str(row["created_at"]),
    )


# ---------------------------------------------------------------------------
# Frontend Entrypoint & Settings Helpers
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def serve_spa():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="static/index.html not found.")
    return FileResponse(str(index_path))


class ApiKeyPayload(BaseModel):
    api_key: str = ""


@app.get("/api/settings/status", tags=["Settings"])
def get_settings_status():
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    masked = None
    if env_key:
        masked = f"{env_key[:4]}...{env_key[-4:]}" if len(env_key) > 8 else "****"
    return {
        "has_env_key": bool(env_key),
        "masked_key": masked,
        "model": get_active_gemini_model(),
    }


@app.post("/api/settings/api-key", tags=["Settings"])
def update_api_key(payload: ApiKeyPayload):
    cleaned = payload.api_key.strip()
    if "\n" in cleaned or "\r" in cleaned:
        raise HTTPException(
            status_code=400,
            detail="API key must be a single line without newline characters.",
        )
    os.environ["GEMINI_API_KEY"] = cleaned
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ENV_FILE.exists():
        ENV_FILE.write_text("", encoding="utf-8")
    set_key(str(ENV_FILE), "GEMINI_API_KEY", cleaned)
    if os.name != "nt":
        try:
            os.chmod(ENV_FILE, 0o600)
        except OSError:
            pass
    return get_settings_status()


@app.post("/api/settings/test-key", tags=["Settings"])
async def verify_api_key_endpoint(
    payload: ApiKeyPayload | None = None,
    x_gemini_api_key: str | None = Header(default=None, alias="X-Gemini-Api-Key"),
):
    candidate = (
        (payload.api_key if payload and payload.api_key.strip() else None)
        or x_gemini_api_key
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip()
    if not candidate:
        raise HTTPException(
            status_code=400,
            detail="No API key entered or configured in .env.",
        )
    try:
        return await anyio.to_thread.run_sync(lambda: test_gemini_api_key(candidate))
    except Exception as exc:
        safe_err = _redact_secret(str(exc), candidate)
        raise HTTPException(
            status_code=400,
            detail=f"Gemini API key test failed: {safe_err}",
        ) from exc


# ---------------------------------------------------------------------------
# AI Timetable Scan & Promo Schedule CRUD
# ---------------------------------------------------------------------------


@app.post(
    "/api/promos/scan",
    response_model=ScanStagingResponse,
    tags=["Promos & Schedules"],
)
async def scan_promo_timetable(
    file: UploadFile = File(...),
    x_gemini_api_key: str | None = Header(default=None, alias="X-Gemini-Api-Key"),
    gemini_api_key_hdr: str | None = Header(default=None, alias="GEMINI-API-KEY"),
    gemini_api_key_raw: str | None = Header(
        default=None, alias="GEMINI_API_KEY", convert_underscores=False
    ),
):
    """Parse an uploaded PDF or image timetable via Gemini 3.8 Flash (Interactions API) without saving to DB."""
    file_bytes = await file.read(MAX_TIMETABLE_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(file_bytes) > MAX_TIMETABLE_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Uploaded timetable exceeds the 25 MB size limit.",
        )

    effective_key = (
        x_gemini_api_key
        or gemini_api_key_hdr
        or gemini_api_key_raw
        or os.getenv("GEMINI_API_KEY")
        or ""
    ).strip()

    try:
        return await anyio.to_thread.run_sync(
            lambda: parse_timetable_bytes(
                file_bytes=file_bytes,
                mime_type=file.content_type or "",
                api_key=effective_key,
                filename=file.filename,
            )
        )
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        msg = str(exc)
        code = 415 if "Unsupported file type" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    except Exception as exc:
        err_str = _redact_secret(str(exc), effective_key)
        if "API_KEY_INVALID" in err_str or "401" in err_str or "403" in err_str:
            raise HTTPException(
                status_code=401,
                detail=f"Gemini API authentication failed. Please check your GEMINI_API_KEY. ({err_str})",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail=f"Gemini timetable extraction failed: {err_str}",
        ) from exc


@app.get("/api/promos", response_model=list[PromoRead], tags=["Promos & Schedules"])
def list_promos(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT id, name, academic_year, created_at FROM promos ORDER BY name COLLATE NOCASE ASC"
    ).fetchall()
    return [_build_promo_read(conn, row) for row in rows]


@app.post(
    "/api/promos",
    response_model=PromoWithSlotsRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Promos & Schedules"],
)
def create_promo(payload: PromoCreate, conn: sqlite3.Connection = Depends(get_db)):
    existing = conn.execute(
        "SELECT id, name, academic_year, created_at FROM promos WHERE LOWER(name) = LOWER(?)",
        (payload.name,),
    ).fetchone()

    if existing and not (payload.overwrite_if_exists or payload.merge_slots):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Promo '{existing['name']}' already exists (id={existing['id']}). "
                "Enable merge_slots or overwrite_if_exists to update its schedule slots without losing members."
            ),
        )

    if existing and (payload.overwrite_if_exists or payload.merge_slots):
        promo_id = int(existing["id"])
        new_year = (
            payload.academic_year
            if payload.academic_year is not None
            else existing["academic_year"]
        )
        conn.execute(
            "UPDATE promos SET name = ?, academic_year = ? WHERE id = ?",
            (payload.name, new_year, promo_id),
        )
        if payload.merge_slots:
            # Replace only the specific non-ALL group(s) present in this upload, and deduplicate ALL Cours slots
            incoming_groups = {
                s.group_target for s in payload.schedule_slots if s.group_target != "ALL"
            }
            for grp in incoming_groups:
                conn.execute(
                    "DELETE FROM schedule_slots WHERE promo_id = ? AND group_target = ?",
                    (promo_id, grp),
                )
            if any(s.group_target == "ALL" for s in payload.schedule_slots):
                conn.execute(
                    "DELETE FROM schedule_slots WHERE promo_id = ? AND group_target = 'ALL'",
                    (promo_id,),
                )
        else:
            conn.execute("DELETE FROM schedule_slots WHERE promo_id = ?", (promo_id,))
    else:
        cursor = conn.execute(
            "INSERT INTO promos (name, academic_year) VALUES (?, ?)",
            (payload.name, payload.academic_year),
        )
        promo_id = int(cursor.lastrowid)

    existing_rows = conn.execute(
        "SELECT day, time_slot, group_target, session_type, module_name, room FROM schedule_slots WHERE promo_id = ?",
        (promo_id,),
    ).fetchall()
    seen_keys: set[tuple[str, str, str, str, str | None, str | None]] = {
        (r["day"], r["time_slot"], r["group_target"], r["session_type"], r["module_name"], r["room"])
        for r in existing_rows
    }
    slots_to_insert: list[tuple[int, str, str, str, str, str | None, str | None]] = []
    for s in payload.schedule_slots:
        dedup_key = (
            s.day,
            s.time_slot,
            s.group_target,
            s.session_type,
            s.module_name,
            s.room,
        )
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            slots_to_insert.append(
                (
                    promo_id,
                    s.day,
                    s.time_slot,
                    s.group_target,
                    s.session_type,
                    s.module_name,
                    s.room,
                )
            )

    if slots_to_insert:
        conn.executemany(
            """
            INSERT INTO schedule_slots
                (promo_id, day, time_slot, group_target, session_type, module_name, room)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            slots_to_insert,
        )

    promo_row = conn.execute(
        "SELECT id, name, academic_year, created_at FROM promos WHERE id = ?",
        (promo_id,),
    ).fetchone()
    slot_rows = conn.execute(
        "SELECT * FROM schedule_slots WHERE promo_id = ?", (promo_id,)
    ).fetchall()
    base_info = _build_promo_read(conn, promo_row)
    return PromoWithSlotsRead(
        **base_info.model_dump(),
        schedule_slots=_sort_slot_rows(slot_rows),
    )


@app.put("/api/promos/{promo_id}", response_model=PromoRead, tags=["Promos & Schedules"])
def update_promo(
    promo_id: int,
    payload: PromoUpdate,
    conn: sqlite3.Connection = Depends(get_db),
):
    existing = conn.execute(
        "SELECT id, name, academic_year, created_at FROM promos WHERE id = ?",
        (promo_id,),
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")

    updates = payload.model_dump(exclude_unset=True)
    new_name = updates["name"] if updates.get("name") is not None else existing["name"]
    new_year = updates.get("academic_year", existing["academic_year"])

    conflict = conn.execute(
        "SELECT id FROM promos WHERE LOWER(name) = LOWER(?) AND id != ?",
        (new_name, promo_id),
    ).fetchone()
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"Another promo named '{new_name}' already exists.",
        )

    conn.execute(
        "UPDATE promos SET name = ?, academic_year = ? WHERE id = ?",
        (new_name, new_year, promo_id),
    )
    updated_row = conn.execute(
        "SELECT id, name, academic_year, created_at FROM promos WHERE id = ?",
        (promo_id,),
    ).fetchone()
    return _build_promo_read(conn, updated_row)


@app.get(
    "/api/promos/{promo_id}/slots",
    response_model=list[ScheduleSlotRead],
    tags=["Promos & Schedules"],
)
def get_promo_slots(promo_id: int, conn: sqlite3.Connection = Depends(get_db)):
    promo = conn.execute("SELECT id FROM promos WHERE id = ?", (promo_id,)).fetchone()
    if not promo:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")
    rows = conn.execute(
        "SELECT * FROM schedule_slots WHERE promo_id = ?", (promo_id,)
    ).fetchall()
    return _sort_slot_rows(rows)


@app.post(
    "/api/promos/{promo_id}/slots",
    response_model=ScheduleSlotRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Promos & Schedules"],
)
def add_promo_slot(
    promo_id: int,
    payload: ScheduleSlotCreate,
    conn: sqlite3.Connection = Depends(get_db),
):
    promo = conn.execute("SELECT id FROM promos WHERE id = ?", (promo_id,)).fetchone()
    if not promo:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")
    cursor = conn.execute(
        """
        INSERT INTO schedule_slots
            (promo_id, day, time_slot, group_target, session_type, module_name, room)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            promo_id,
            payload.day,
            payload.time_slot,
            payload.group_target,
            payload.session_type,
            payload.module_name,
            payload.room,
        ),
    )
    row = conn.execute(
        "SELECT * FROM schedule_slots WHERE id = ?", (int(cursor.lastrowid),)
    ).fetchone()
    return ScheduleSlotRead(**dict(row))


@app.put(
    "/api/slots/{slot_id}",
    response_model=ScheduleSlotRead,
    tags=["Promos & Schedules"],
)
def update_slot(
    slot_id: int,
    payload: ScheduleSlotUpdate,
    conn: sqlite3.Connection = Depends(get_db),
):
    existing = conn.execute(
        "SELECT * FROM schedule_slots WHERE id = ?", (slot_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Schedule slot {slot_id} not found.")

    updates = payload.model_dump(exclude_unset=True)
    new_day = updates["day"] if updates.get("day") is not None else existing["day"]
    new_time_slot = (
        updates["time_slot"] if updates.get("time_slot") is not None else existing["time_slot"]
    )
    new_group = (
        updates["group_target"]
        if updates.get("group_target") is not None
        else existing["group_target"]
    )
    new_type = (
        updates["session_type"]
        if updates.get("session_type") is not None
        else existing["session_type"]
    )
    new_module = updates.get("module_name", existing["module_name"])
    new_room = updates.get("room", existing["room"])

    conn.execute(
        """
        UPDATE schedule_slots
        SET day = ?, time_slot = ?, group_target = ?, session_type = ?, module_name = ?, room = ?
        WHERE id = ?
        """,
        (new_day, new_time_slot, new_group, new_type, new_module, new_room, slot_id),
    )
    updated_row = conn.execute(
        "SELECT * FROM schedule_slots WHERE id = ?", (slot_id,)
    ).fetchone()
    return ScheduleSlotRead(**dict(updated_row))


@app.delete("/api/slots/{slot_id}", tags=["Promos & Schedules"])
def delete_slot(slot_id: int, conn: sqlite3.Connection = Depends(get_db)):
    existing = conn.execute(
        "SELECT id FROM schedule_slots WHERE id = ?", (slot_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Schedule slot {slot_id} not found.")
    conn.execute("DELETE FROM schedule_slots WHERE id = ?", (slot_id,))
    return {"deleted": True, "slot_id": slot_id}


@app.delete("/api/promos/{promo_id}", tags=["Promos & Schedules"])
def delete_promo(promo_id: int, conn: sqlite3.Connection = Depends(get_db)):
    existing = conn.execute(
        "SELECT id, name FROM promos WHERE id = ?", (promo_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")
    conn.execute("DELETE FROM promos WHERE id = ?", (promo_id,))
    return {"deleted": True, "promo_id": promo_id, "name": existing["name"]}


@app.get(
    "/api/promos/{promo_id}/members",
    response_model=list[MemberRead],
    tags=["Promos & Schedules"],
)
def get_promo_members(promo_id: int, conn: sqlite3.Connection = Depends(get_db)):
    """Fetch all members belonging to a specific academic promo, annotated with timetable slot linkage."""
    promo = conn.execute("SELECT id, name FROM promos WHERE id = ?", (promo_id,)).fetchone()
    if not promo:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")

    rows = conn.execute(
        """
        SELECT
            m.id, m.full_name, m.department, m.role, m.promo_id,
            p.name AS promo_name, m.group_name, m.discord, m.phone,
            m.email, m.is_active, m.shift_exempt, m.notes, m.created_at
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        WHERE m.promo_id = ?
        ORDER BY m.is_active DESC, m.group_name ASC, m.full_name COLLATE NOCASE ASC
        """,
        (promo_id,),
    ).fetchall()
    return _build_annotated_members(conn, rows)


@app.post(
    "/api/promos/{promo_id}/members/assign",
    response_model=BulkAssignResult,
    tags=["Promos & Schedules"],
)
def assign_members_to_promo(
    promo_id: int,
    payload: PromoAssignMembersPayload,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Assign/reassign existing saved club members to this promo and optionally update their academic group."""
    promo = conn.execute("SELECT id, name FROM promos WHERE id = ?", (promo_id,)).fetchone()
    if not promo:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")

    member_ids = list(dict.fromkeys(payload.member_ids))
    if not member_ids:
        return BulkAssignResult(
            promo_id=promo_id,
            promo_name=promo["name"],
            assigned_count=0,
            members=[],
        )

    placeholders = ",".join("?" for _ in member_ids)
    if payload.group_name:
        norm_group = normalize_group_code(payload.group_name, allow_all=False)
        conn.execute(
            f"""
            UPDATE members
            SET promo_id = ?, group_name = ?
            WHERE id IN ({placeholders})
            """,
            [promo_id, norm_group, *member_ids],
        )
    else:
        conn.execute(
            f"""
            UPDATE members
            SET promo_id = ?
            WHERE id IN ({placeholders})
            """,
            [promo_id, *member_ids],
        )

    updated_rows = conn.execute(
        f"""
        SELECT
            m.id, m.full_name, m.department, m.role, m.promo_id,
            p.name AS promo_name, m.group_name, m.discord, m.phone,
            m.email, m.is_active, m.shift_exempt, m.notes, m.created_at
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        WHERE m.id IN ({placeholders})
        ORDER BY m.full_name COLLATE NOCASE ASC
        """,
        member_ids,
    ).fetchall()

    annotated = _build_annotated_members(conn, updated_rows)
    return BulkAssignResult(
        promo_id=promo_id,
        promo_name=promo["name"],
        assigned_count=len(updated_rows),
        members=annotated,
    )


@app.post(
    "/api/promos/{promo_id}/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Promos & Schedules"],
)
def quick_add_promo_member(
    promo_id: int,
    payload: PromoMemberQuickCreate,
    conn: sqlite3.Connection = Depends(get_db),
):
    """Quickly create a new member bound directly to this promo and group."""
    promo = conn.execute("SELECT id, name FROM promos WHERE id = ?", (promo_id,)).fetchone()
    if not promo:
        raise HTTPException(status_code=404, detail=f"Promo {promo_id} not found.")

    cursor = conn.execute(
        """
        INSERT INTO members (
            full_name, department, role, promo_id, group_name,
            discord, phone, email, is_active, shift_exempt, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.full_name,
            payload.department,
            payload.role,
            promo_id,
            payload.group_name,
            payload.discord,
            payload.phone,
            payload.email,
            1 if payload.is_active else 0,
            1 if payload.shift_exempt else 0,
            payload.notes,
        ),
    )
    return _fetch_member_read(conn, int(cursor.lastrowid))


# ---------------------------------------------------------------------------
# Rich Member Directory & Deduplicated CSV Import / Export
# ---------------------------------------------------------------------------


@app.get("/api/members", response_model=list[MemberRead], tags=["Members"])
def list_members(
    department: str | None = Query(default=None),
    promo_id: int | None = Query(default=None),
    group_name: str | None = Query(default=None),
    search: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_db),
):
    clauses: list[str] = []
    params: list[object] = []

    if active_only:
        clauses.append("m.is_active = 1")

    if department and department.strip() and department.strip().upper() != "ALL":
        clauses.append("LOWER(m.department) = LOWER(?)")
        params.append(department.strip())

    if promo_id is not None:
        clauses.append("m.promo_id = ?")
        params.append(promo_id)

    if group_name and group_name.strip():
        try:
            norm_group = normalize_group_code(group_name, allow_all=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        clauses.append("m.group_name = ?")
        params.append(norm_group)

    if search and search.strip():
        q = f"%{search.strip()}%"
        clauses.append(
            "("
            "m.full_name LIKE ? COLLATE NOCASE OR "
            "p.name LIKE ? COLLATE NOCASE OR "
            "m.group_name LIKE ? COLLATE NOCASE OR "
            "m.department LIKE ? COLLATE NOCASE OR "
            "m.role LIKE ? COLLATE NOCASE OR "
            "COALESCE(m.discord, '') LIKE ? COLLATE NOCASE OR "
            "COALESCE(m.phone, '') LIKE ? COLLATE NOCASE"
            ")"
        )
        params.extend([q, q, q, q, q, q, q])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            m.id, m.full_name, m.department, m.role, m.promo_id,
            p.name AS promo_name, m.group_name, m.discord, m.phone,
            m.email, m.is_active, m.shift_exempt, m.notes, m.created_at
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        {where_sql}
        ORDER BY m.is_active DESC, m.full_name COLLATE NOCASE ASC
        """,
        params,
    ).fetchall()
    return _build_annotated_members(conn, rows)


@app.post(
    "/api/members",
    response_model=MemberRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Members"],
)
def create_member(payload: MemberCreate, conn: sqlite3.Connection = Depends(get_db)):
    promo = conn.execute(
        "SELECT id FROM promos WHERE id = ?", (payload.promo_id,)
    ).fetchone()
    if not promo:
        raise HTTPException(
            status_code=404, detail=f"Promo {payload.promo_id} not found."
        )

    if not payload.allow_duplicate:
        dup = conn.execute(
            "SELECT id, full_name FROM members WHERE LOWER(full_name) = LOWER(?) AND promo_id = ?",
            (payload.full_name, payload.promo_id),
        ).fetchone()
        if dup:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Member '{dup['full_name']}' already exists in this promo. "
                    "Edit the existing member or enable allow_duplicate if they are two different people."
                ),
            )

    cursor = conn.execute(
        """
        INSERT INTO members (
            full_name, department, role, promo_id, group_name,
            discord, phone, email, is_active, shift_exempt, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.full_name,
            payload.department,
            payload.role,
            payload.promo_id,
            payload.group_name,
            payload.discord,
            payload.phone,
            payload.email,
            1 if payload.is_active else 0,
            1 if payload.shift_exempt else 0,
            payload.notes,
        ),
    )
    return _fetch_member_read(conn, int(cursor.lastrowid))


@app.put("/api/members/{member_id}", response_model=MemberRead, tags=["Members"])
def update_member(
    member_id: int,
    payload: MemberUpdate,
    conn: sqlite3.Connection = Depends(get_db),
):
    existing = conn.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")

    updates = payload.model_dump(exclude_unset=True)
    new_name = (
        updates["full_name"] if updates.get("full_name") is not None else existing["full_name"]
    )
    new_dept = (
        updates["department"] if updates.get("department") is not None else existing["department"]
    )
    new_role = (
        updates["role"] if updates.get("role") is not None else (existing["role"] or "Member")
    )
    new_promo_id = (
        updates["promo_id"] if updates.get("promo_id") is not None else existing["promo_id"]
    )
    new_group = (
        updates["group_name"] if updates.get("group_name") is not None else existing["group_name"]
    )
    new_discord = updates.get("discord", existing["discord"])
    new_phone = updates.get("phone", existing["phone"])
    new_email = updates.get("email", existing["email"])
    new_active = (
        (1 if updates["is_active"] else 0)
        if ("is_active" in updates and updates["is_active"] is not None)
        else int(existing["is_active"])
    )
    new_exempt = (
        (1 if updates["shift_exempt"] else 0)
        if ("shift_exempt" in updates and updates["shift_exempt"] is not None)
        else int(existing["shift_exempt"])
    )
    new_notes = updates.get("notes", existing["notes"])

    if new_promo_id != existing["promo_id"]:
        promo = conn.execute(
            "SELECT id FROM promos WHERE id = ?", (new_promo_id,)
        ).fetchone()
        if not promo:
            raise HTTPException(
                status_code=404, detail=f"Promo {new_promo_id} not found."
            )

    conn.execute(
        """
        UPDATE members
        SET full_name = ?, department = ?, role = ?, promo_id = ?, group_name = ?,
            discord = ?, phone = ?, email = ?, is_active = ?, shift_exempt = ?, notes = ?
        WHERE id = ?
        """,
        (
            new_name,
            new_dept,
            new_role,
            new_promo_id,
            new_group,
            new_discord,
            new_phone,
            new_email,
            new_active,
            new_exempt,
            new_notes,
            member_id,
        ),
    )
    return _fetch_member_read(conn, member_id)


@app.delete("/api/members/{member_id}", tags=["Members"])
def delete_member(member_id: int, conn: sqlite3.Connection = Depends(get_db)):
    existing = conn.execute(
        "SELECT id, full_name FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Member {member_id} not found.")
    conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
    return {"deleted": True, "member_id": member_id, "full_name": existing["full_name"]}


@app.get("/api/members/export-csv", tags=["Members"])
def export_members_csv(conn: sqlite3.Connection = Depends(get_db)):
    """Export the complete member directory as an Excel-friendly UTF-8-BOM CSV file."""
    rows = conn.execute(
        """
        SELECT
            m.full_name, m.department, m.role, p.name AS promo_name, m.group_name,
            m.discord, m.phone, m.email, m.is_active, m.shift_exempt, m.notes
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        ORDER BY m.department COLLATE NOCASE ASC, m.full_name COLLATE NOCASE ASC
        """
    ).fetchall()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "full_name",
            "department",
            "role",
            "promo_name",
            "group_name",
            "discord",
            "phone",
            "email",
            "is_active",
            "shift_exempt",
            "notes",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["full_name"],
                r["department"],
                r["role"] or "Member",
                r["promo_name"],
                r["group_name"],
                r["discord"] or "",
                r["phone"] or "",
                r["email"] or "",
                "true" if r["is_active"] else "false",
                "true" if r["shift_exempt"] else "false",
                r["notes"] or "",
            ]
        )

    csv_bytes = buf.getvalue().encode("utf-8-sig")
    filename = f"club_members_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post(
    "/api/members/bulk-csv",
    response_model=BulkCSVImportResult,
    tags=["Members"],
)
async def bulk_import_members_csv(
    file: UploadFile = File(...),
    auto_create_promos: bool = Query(default=True),
    on_duplicate: Literal["update", "skip"] = Query(default="update"),
    conn: sqlite3.Connection = Depends(get_db),
):
    raw_bytes = await file.read(MAX_IMPORT_UPLOAD_BYTES + 1)
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded CSV file is empty.")
    if len(raw_bytes) > MAX_IMPORT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Uploaded CSV file exceeds the 10 MB size limit.",
        )

    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw_bytes.decode("latin-1")

    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="CSV contains no non-empty rows.")

    sample = "\n".join(lines[:10])
    delimiter = ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        if sample.count(";") > sample.count(","):
            delimiter = ";"

    reader = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    if not reader:
        raise HTTPException(status_code=400, detail="Could not parse CSV rows.")

    first_row_lower = [c.strip().lower() for c in reader[0]]
    header_aliases: dict[str, set[str]] = {
        "full_name": {"full_name", "fullname", "name", "nom", "nom_complet", "member"},
        "department": {"department", "dept", "departement", "département", "pole", "pôle"},
        "role": {"role", "rôle", "poste", "position", "title"},
        "promo_name": {"promo_name", "promo", "section", "promotion", "specialite", "spécialité"},
        "group_name": {"group_name", "group", "groupe", "grp", "td_group"},
        "discord": {"discord", "discord_handle", "pseudo_discord", "handle"},
        "phone": {"phone", "tel", "telephone", "téléphone", "mobile"},
        "email": {"email", "mail", "e-mail", "courriel"},
        "is_active": {"is_active", "active", "actif", "status"},
        "shift_exempt": {"shift_exempt", "exempt", "dispense", "stand_exempt"},
        "notes": {"notes", "note", "remarque", "comment"},
    }

    col_idx: dict[str, int] = {}
    for field_key, aliases in header_aliases.items():
        for idx, col in enumerate(first_row_lower):
            if col in aliases:
                col_idx[field_key] = idx
                break

    required_keys = ("full_name", "department", "promo_name", "group_name")
    if sum(1 for k in required_keys if k in col_idx) >= 3:
        data_rows = reader[1:]
        start_line_num = 2
        if sum(1 for k in required_keys if k in col_idx) < 4 and len(first_row_lower) >= 4:
            used = set(col_idx.values())
            remaining_cols = [i for i in range(len(first_row_lower)) if i not in used]
            for k in required_keys:
                if k not in col_idx and remaining_cols:
                    col_idx[k] = remaining_cols.pop(0)
    else:
        col_idx = {"full_name": 0, "department": 1, "promo_name": 2, "group_name": 3}
        data_rows = reader
        start_line_num = 1

    promo_rows = conn.execute("SELECT id, name FROM promos").fetchall()
    promo_lookup: dict[str, int] = {
        r["name"].strip().lower(): int(r["id"]) for r in promo_rows
    }

    # Index existing members for deduplication
    existing_rows = conn.execute(
        "SELECT id, full_name, promo_id, email FROM members"
    ).fetchall()
    by_email: dict[str, int] = {}
    by_name_promo: dict[tuple[str, int], int] = {}
    by_unique_name: dict[str, list[int]] = {}
    for er in existing_rows:
        mid = int(er["id"])
        nl = er["full_name"].strip().lower()
        by_name_promo[(nl, int(er["promo_id"]))] = mid
        by_unique_name.setdefault(nl, []).append(mid)
        if er["email"] and er["email"].strip():
            by_email[er["email"].strip().lower()] = mid

    created_promos: list[str] = []
    errors: list[str] = []
    touched_members: list[MemberRead] = []
    imported_count = 0
    updated_count = 0
    skipped_count = 0

    def _cell(row_vals: list[str], key: str) -> str:
        idx = col_idx.get(key)
        if idx is None or idx >= len(row_vals):
            return ""
        return row_vals[idx].strip()

    for offset, row in enumerate(data_rows):
        line_no = start_line_num + offset
        if not any(cell.strip() for cell in row):
            continue

        req_max = max(col_idx[k] for k in required_keys if k in col_idx)
        if req_max >= len(row):
            skipped_count += 1
            errors.append(
                f"Line {line_no}: expected at least {req_max + 1} columns, got {len(row)}."
            )
            continue

        raw_name = re.sub(r"\s+", " ", _cell(row, "full_name"))
        raw_dept = _cell(row, "department")
        raw_promo = re.sub(r"\s+", " ", _cell(row, "promo_name"))
        raw_group = _cell(row, "group_name")

        if not raw_name or not raw_promo or not raw_group:
            skipped_count += 1
            errors.append(
                f"Line {line_no}: missing required full_name, promo_name, or group_name."
            )
            continue

        try:
            norm_dept = normalize_department(raw_dept or "General")
            norm_group = normalize_group_code(raw_group, allow_all=False)
            norm_role = normalize_role(_cell(row, "role") or "Member")
        except ValueError as exc:
            skipped_count += 1
            errors.append(f"Line {line_no} ({raw_name}): {exc}")
            continue

        promo_key = raw_promo.lower()
        promo_id = promo_lookup.get(promo_key)
        if promo_id is None:
            if auto_create_promos:
                cur = conn.execute("INSERT INTO promos (name) VALUES (?)", (raw_promo,))
                promo_id = int(cur.lastrowid)
                promo_lookup[promo_key] = promo_id
                created_promos.append(raw_promo)
            else:
                skipped_count += 1
                errors.append(
                    f"Line {line_no} ({raw_name}): Promo '{raw_promo}' does not exist."
                )
                continue

        discord_val = _cell(row, "discord") or None
        phone_val = _cell(row, "phone") or None
        email_val = _cell(row, "email") or None
        notes_val = _cell(row, "notes") or None
        is_active_val = _parse_bool_cell(_cell(row, "is_active"), True)
        shift_exempt_val = _parse_bool_cell(_cell(row, "shift_exempt"), False)

        # Match existing member
        name_lower = raw_name.lower()
        matched_id: int | None = None
        if email_val and email_val.lower() in by_email:
            matched_id = by_email[email_val.lower()]
        elif (name_lower, promo_id) in by_name_promo:
            matched_id = by_name_promo[(name_lower, promo_id)]
        elif len(by_unique_name.get(name_lower, [])) == 1:
            matched_id = by_unique_name[name_lower][0]

        if matched_id is not None:
            if on_duplicate == "skip":
                skipped_count += 1
                continue

            conn.execute(
                """
                UPDATE members
                SET full_name = ?, department = ?, role = ?, promo_id = ?, group_name = ?,
                    discord = COALESCE(?, discord),
                    phone = COALESCE(?, phone),
                    email = COALESCE(?, email),
                    is_active = ?, shift_exempt = ?,
                    notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (
                    raw_name,
                    norm_dept,
                    norm_role,
                    promo_id,
                    norm_group,
                    discord_val,
                    phone_val,
                    email_val,
                    1 if is_active_val else 0,
                    1 if shift_exempt_val else 0,
                    notes_val,
                    matched_id,
                ),
            )
            by_name_promo[(name_lower, promo_id)] = matched_id
            if email_val:
                by_email[email_val.lower()] = matched_id
            updated_count += 1
            touched_members.append(_fetch_member_read(conn, matched_id))
        else:
            cur = conn.execute(
                """
                INSERT INTO members (
                    full_name, department, role, promo_id, group_name,
                    discord, phone, email, is_active, shift_exempt, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw_name,
                    norm_dept,
                    norm_role,
                    promo_id,
                    norm_group,
                    discord_val,
                    phone_val,
                    email_val,
                    1 if is_active_val else 0,
                    1 if shift_exempt_val else 0,
                    notes_val,
                ),
            )
            new_id = int(cur.lastrowid)
            by_name_promo[(name_lower, promo_id)] = new_id
            by_unique_name.setdefault(name_lower, []).append(new_id)
            if email_val:
                by_email[email_val.lower()] = new_id
            imported_count += 1
            touched_members.append(_fetch_member_read(conn, new_id))

    return BulkCSVImportResult(
        imported_count=imported_count,
        updated_count=updated_count,
        created_promos=created_promos,
        skipped_count=skipped_count,
        errors=errors,
        members=touched_members,
    )


# ---------------------------------------------------------------------------
# Availability Engine, Heatmap & Shift Generation Endpoints
# ---------------------------------------------------------------------------


@app.get(
    "/api/analysis/slot",
    response_model=SlotBreakdownResponse,
    tags=["Availability & Shifts"],
)
def analyze_slot(
    day: str = Query(..., description="Canonical day (Saturday..Thursday)"),
    time_slot: str = Query(..., description="Canonical 90-min slot (e.g. 08:00-09:30)"),
    department: str | None = Query(default=None),
    promo_id: int | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        return get_slot_breakdown(
            conn=conn,
            day=day,
            time_slot=time_slot,
            department=department,
            promo_id=promo_id,
            include_inactive=include_inactive,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/api/analysis/heatmap",
    response_model=HeatmapResponse,
    tags=["Availability & Shifts"],
)
def analyze_heatmap(
    department: str | None = Query(default=None),
    promo_id: int | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_db),
):
    return generate_weekly_heatmap(
        conn=conn,
        department=department,
        promo_id=promo_id,
        include_inactive=include_inactive,
    )


@app.post(
    "/api/analysis/shifts",
    response_model=ShiftScheduleResponse,
    tags=["Availability & Shifts"],
)
def create_exhibition_shifts(
    payload: ShiftRequest,
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        return generate_exhibition_shifts(
            conn=conn,
            day=payload.day,
            time_slots=payload.time_slots,
            members_per_shift=payload.members_per_shift,
            department=payload.department,
            promo_id=payload.promo_id,
            allow_cours_fallback=payload.allow_cours_fallback,
            include_shift_exempt=payload.include_shift_exempt,
            excluded_member_ids=payload.excluded_member_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Saved Shift Plans CRUD (SQLite Persisted Rotations)
# ---------------------------------------------------------------------------


@app.get(
    "/api/shift-plans",
    response_model=list[SavedShiftPlanRead],
    tags=["Saved Shift Plans"],
)
def list_saved_shift_plans(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT * FROM saved_shift_plans ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    return [_row_to_shift_plan_read(r) for r in rows]


@app.post(
    "/api/shift-plans",
    response_model=SavedShiftPlanRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Saved Shift Plans"],
)
def create_saved_shift_plan(
    payload: SavedShiftPlanCreate,
    conn: sqlite3.Connection = Depends(get_db),
):
    sched = payload.schedule
    if sched.promo_id is not None:
        promo = conn.execute(
            "SELECT id FROM promos WHERE id = ?", (sched.promo_id,)
        ).fetchone()
        if not promo:
            raise HTTPException(status_code=404, detail=f"Promo {sched.promo_id} not found.")
    cur = conn.execute(
        """
        INSERT INTO saved_shift_plans (
            title, event_date, day, department, promo_id,
            members_per_shift, allow_cours_fallback, notes, schedule_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.title,
            payload.event_date,
            sched.day,
            sched.department,
            sched.promo_id,
            sched.members_per_shift,
            1 if sched.allow_cours_fallback else 0,
            payload.notes,
            sched.model_dump_json(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM saved_shift_plans WHERE id = ?", (int(cur.lastrowid),)
    ).fetchone()
    return _row_to_shift_plan_read(row)


@app.get(
    "/api/shift-plans/{plan_id}",
    response_model=SavedShiftPlanRead,
    tags=["Saved Shift Plans"],
)
def get_saved_shift_plan(plan_id: int, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM saved_shift_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Shift plan {plan_id} not found.")
    return _row_to_shift_plan_read(row)


@app.put(
    "/api/shift-plans/{plan_id}",
    response_model=SavedShiftPlanRead,
    tags=["Saved Shift Plans"],
)
def update_saved_shift_plan(
    plan_id: int,
    payload: SavedShiftPlanUpdate,
    conn: sqlite3.Connection = Depends(get_db),
):
    existing = conn.execute(
        "SELECT * FROM saved_shift_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Shift plan {plan_id} not found.")

    updates = payload.model_dump(exclude_unset=True)
    new_title = updates["title"] if updates.get("title") is not None else existing["title"]
    new_date = updates.get("event_date", existing["event_date"])
    new_notes = updates.get("notes", existing["notes"])

    if payload.schedule is not None:
        sched = payload.schedule
        if sched.promo_id is not None:
            promo = conn.execute(
                "SELECT id FROM promos WHERE id = ?", (sched.promo_id,)
            ).fetchone()
            if not promo:
                raise HTTPException(status_code=404, detail=f"Promo {sched.promo_id} not found.")
        sched_json = sched.model_dump_json()
        day_val = sched.day
        dept_val = sched.department
        promo_val = sched.promo_id
        mps_val = sched.members_per_shift
        cours_val = 1 if sched.allow_cours_fallback else 0
    else:
        sched_json = existing["schedule_json"]
        day_val = existing["day"]
        dept_val = existing["department"]
        promo_val = existing["promo_id"]
        mps_val = existing["members_per_shift"]
        cours_val = existing["allow_cours_fallback"]

    conn.execute(
        """
        UPDATE saved_shift_plans
        SET title = ?, event_date = ?, day = ?, department = ?, promo_id = ?,
            members_per_shift = ?, allow_cours_fallback = ?, notes = ?,
            schedule_json = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            new_title,
            new_date,
            day_val,
            dept_val,
            promo_val,
            mps_val,
            cours_val,
            new_notes,
            sched_json,
            plan_id,
        ),
    )
    row = conn.execute(
        "SELECT * FROM saved_shift_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    return _row_to_shift_plan_read(row)


@app.delete("/api/shift-plans/{plan_id}", tags=["Saved Shift Plans"])
def delete_saved_shift_plan(plan_id: int, conn: sqlite3.Connection = Depends(get_db)):
    existing = conn.execute(
        "SELECT id, title FROM saved_shift_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Shift plan {plan_id} not found.")
    conn.execute("DELETE FROM saved_shift_plans WHERE id = ?", (plan_id,))
    return {"deleted": True, "plan_id": plan_id, "title": existing["title"]}


# ---------------------------------------------------------------------------
# Saved Meetings CRUD
# ---------------------------------------------------------------------------


@app.get(
    "/api/meetings",
    response_model=list[SavedMeetingRead],
    tags=["Saved Meetings"],
)
def list_saved_meetings(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT * FROM saved_meetings ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [_build_live_meeting_read(conn, r) for r in rows]


@app.post(
    "/api/meetings",
    response_model=SavedMeetingRead,
    status_code=status.HTTP_201_CREATED,
    tags=["Saved Meetings"],
)
def create_saved_meeting(
    payload: SavedMeetingCreate,
    conn: sqlite3.Connection = Depends(get_db),
):
    if payload.promo_id is not None:
        promo = conn.execute(
            "SELECT id FROM promos WHERE id = ?", (payload.promo_id,)
        ).fetchone()
        if not promo:
            raise HTTPException(status_code=404, detail=f"Promo {payload.promo_id} not found.")
    clean_dept = (
        payload.department.strip()
        if payload.department and payload.department.strip().upper() != "ALL"
        else None
    )
    bd = get_slot_breakdown(
        conn=conn,
        day=payload.day,
        time_slot=payload.time_slot,
        department=clean_dept,
        promo_id=payload.promo_id,
        include_inactive=False,
    )
    cur = conn.execute(
        """
        INSERT INTO saved_meetings (
            title, meeting_date, day, time_slot, department, promo_id,
            location, notes, expected_free_count, expected_cours_count,
            total_cohort_count, weighted_score
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.title,
            payload.meeting_date,
            payload.day,
            payload.time_slot,
            clean_dept,
            payload.promo_id,
            payload.location,
            payload.notes,
            bd.strict_free_count,
            bd.counts.get("COURS", 0),
            bd.total_members,
            bd.weighted_score,
        ),
    )
    row = conn.execute(
        "SELECT * FROM saved_meetings WHERE id = ?", (int(cur.lastrowid),)
    ).fetchone()
    return _build_live_meeting_read(conn, row)


@app.delete("/api/meetings/{meeting_id}", tags=["Saved Meetings"])
def delete_saved_meeting(meeting_id: int, conn: sqlite3.Connection = Depends(get_db)):
    existing = conn.execute(
        "SELECT id, title FROM saved_meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found.")
    conn.execute("DELETE FROM saved_meetings WHERE id = ?", (meeting_id,))
    return {"deleted": True, "meeting_id": meeting_id, "title": existing["title"]}


# ---------------------------------------------------------------------------
# Portable Workspace Backup, Restore & Clean Reset
# ---------------------------------------------------------------------------


@app.get("/api/workspace/export", tags=["Workspace & Backup"])
def export_workspace_json(conn: sqlite3.Connection = Depends(get_db)):
    """Export all promos, schedule slots, members, saved shift plans, and meetings as portable JSON."""
    promo_rows = conn.execute(
        "SELECT id, name, academic_year, created_at FROM promos ORDER BY name COLLATE NOCASE ASC"
    ).fetchall()

    exported_promos = []
    for pr in promo_rows:
        slots = conn.execute(
            """
            SELECT day, time_slot, group_target, session_type, module_name, room
            FROM schedule_slots WHERE promo_id = ?
            """,
            (pr["id"],),
        ).fetchall()
        exported_promos.append(
            {
                "name": pr["name"],
                "academic_year": pr["academic_year"],
                "schedule_slots": [dict(s) for s in slots],
            }
        )

    member_rows = conn.execute(
        """
        SELECT
            m.full_name, m.department, m.role, p.name AS promo_name, m.group_name,
            m.discord, m.phone, m.email, m.is_active, m.shift_exempt, m.notes
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        ORDER BY m.full_name COLLATE NOCASE ASC
        """
    ).fetchall()
    exported_members = [
        {
            **dict(mr),
            "is_active": bool(mr["is_active"]),
            "shift_exempt": bool(mr["shift_exempt"]),
        }
        for mr in member_rows
    ]

    shift_rows = conn.execute(
        "SELECT title, event_date, day, department, members_per_shift, allow_cours_fallback, notes, schedule_json FROM saved_shift_plans"
    ).fetchall()
    exported_shifts = [
        {
            "title": sr["title"],
            "event_date": sr["event_date"],
            "day": sr["day"],
            "department": sr["department"],
            "members_per_shift": sr["members_per_shift"],
            "allow_cours_fallback": bool(sr["allow_cours_fallback"]),
            "notes": sr["notes"],
            "schedule": json.loads(sr["schedule_json"]),
        }
        for sr in shift_rows
    ]

    meeting_rows = conn.execute(
        "SELECT title, meeting_date, day, time_slot, department, location, notes FROM saved_meetings"
    ).fetchall()
    exported_meetings = [dict(mtr) for mtr in meeting_rows]

    snapshot = {
        "schema_version": 2,
        "exported_at": datetime.now(UTC).isoformat(),
        "promos": exported_promos,
        "members": exported_members,
        "saved_shift_plans": exported_shifts,
        "saved_meetings": exported_meetings,
    }
    filename = f"club_workspace_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return Response(
        content=json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/workspace/export-db", tags=["Workspace & Backup"])
def export_workspace_sqlite():
    """Download a consistent binary copy of club_scheduler.db."""
    db_bytes = export_sqlite_backup_bytes()
    filename = f"club_scheduler_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
    return Response(
        content=db_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/workspace/import", tags=["Workspace & Backup"])
async def import_workspace_json(
    file: UploadFile = File(...),
    mode: Literal["merge", "replace"] = Query(default="merge"),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Restore or merge a portable workspace JSON snapshot."""
    raw = await file.read(MAX_IMPORT_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="Backup file is empty.")
    if len(raw) > MAX_IMPORT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Backup file exceeds the 10 MB size limit.",
        )
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON backup file: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("promos"), list):
        raise HTTPException(
            status_code=400,
            detail="Invalid workspace snapshot format (missing 'promos' list).",
        )

    if mode == "replace":
        conn.execute("DELETE FROM saved_meetings;")
        conn.execute("DELETE FROM saved_shift_plans;")
        conn.execute("DELETE FROM members;")
        conn.execute("DELETE FROM schedule_slots;")
        conn.execute("DELETE FROM promos;")

    promo_lookup: dict[str, int] = {
        r["name"].strip().lower(): int(r["id"])
        for r in conn.execute("SELECT id, name FROM promos").fetchall()
    }

    promos_imported = 0
    slots_imported = 0
    members_imported = 0
    shifts_imported = 0
    meetings_imported = 0

    try:
        for p_item in data.get("promos", []):
            if not isinstance(p_item, dict):
                raise ValueError("Each promo entry must be an object.")
            p_name = str(p_item.get("name", "")).strip()
            if not p_name:
                continue
            p_year = p_item.get("academic_year")
            key = p_name.lower()
            if key in promo_lookup:
                pid = promo_lookup[key]
                conn.execute("DELETE FROM schedule_slots WHERE promo_id = ?", (pid,))
                conn.execute(
                    "UPDATE promos SET academic_year = COALESCE(?, academic_year) WHERE id = ?",
                    (p_year, pid),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO promos (name, academic_year) VALUES (?, ?)",
                    (p_name, p_year),
                )
                pid = int(cur.lastrowid)
                promo_lookup[key] = pid
            promos_imported += 1

            for s_raw in p_item.get("schedule_slots", []):
                day = normalize_day(s_raw["day"])
                t_slot = normalize_time_slot(s_raw["time_slot"], snap_nearest=True)
                grp = normalize_group_code(s_raw.get("group_target", "ALL"), allow_all=True)
                s_type = normalize_session_type(s_raw["session_type"])
                conn.execute(
                    """
                    INSERT INTO schedule_slots
                        (promo_id, day, time_slot, group_target, session_type, module_name, room)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        day,
                        t_slot,
                        grp,
                        s_type,
                        s_raw.get("module_name"),
                        s_raw.get("room"),
                    ),
                )
                slots_imported += 1

        for m_raw in data.get("members", []):
            if not isinstance(m_raw, dict):
                raise ValueError("Each member entry must be an object.")
            m_name = str(m_raw.get("full_name", "")).strip()
            p_name = str(m_raw.get("promo_name", "")).strip()
            if not m_name or not p_name:
                continue
            pkey = p_name.lower()
            if pkey not in promo_lookup:
                cur = conn.execute("INSERT INTO promos (name) VALUES (?)", (p_name,))
                promo_lookup[pkey] = int(cur.lastrowid)
            pid = promo_lookup[pkey]
            dept = normalize_department(m_raw.get("department", "General"))
            role = normalize_role(m_raw.get("role", "Member"))
            grp = normalize_group_code(m_raw.get("group_name", "G1"), allow_all=False)

            existing_m = conn.execute(
                "SELECT id FROM members WHERE LOWER(full_name) = LOWER(?) AND promo_id = ?",
                (m_name, pid),
            ).fetchone()
            if existing_m:
                conn.execute(
                    """
                    UPDATE members
                    SET department = ?, role = ?, group_name = ?, discord = ?, phone = ?,
                        email = ?, is_active = ?, shift_exempt = ?, notes = ?
                    WHERE id = ?
                    """,
                    (
                        dept,
                        role,
                        grp,
                        m_raw.get("discord"),
                        m_raw.get("phone"),
                        m_raw.get("email"),
                        1 if m_raw.get("is_active", True) else 0,
                        1 if m_raw.get("shift_exempt", False) else 0,
                        m_raw.get("notes"),
                        int(existing_m["id"]),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO members (
                        full_name, department, role, promo_id, group_name,
                        discord, phone, email, is_active, shift_exempt, notes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        m_name,
                        dept,
                        role,
                        pid,
                        grp,
                        m_raw.get("discord"),
                        m_raw.get("phone"),
                        m_raw.get("email"),
                        1 if m_raw.get("is_active", True) else 0,
                        1 if m_raw.get("shift_exempt", False) else 0,
                        m_raw.get("notes"),
                    ),
                )
            members_imported += 1

        for sp in data.get("saved_shift_plans", []):
            if not isinstance(sp, dict) or "schedule" not in sp or not sp.get("title"):
                continue
            validated_sched = ShiftScheduleResponse.model_validate(sp["schedule"])
            norm_day = normalize_day(sp.get("day", validated_sched.day))
            conn.execute(
                """
                INSERT INTO saved_shift_plans (
                    title, event_date, day, department, members_per_shift,
                    allow_cours_fallback, notes, schedule_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(sp["title"]).strip(),
                    sp.get("event_date"),
                    norm_day,
                    sp.get("department"),
                    int(sp.get("members_per_shift", validated_sched.members_per_shift)),
                    1 if sp.get("allow_cours_fallback", validated_sched.allow_cours_fallback) else 0,
                    sp.get("notes"),
                    validated_sched.model_dump_json(),
                ),
            )
            shifts_imported += 1

        for mt in data.get("saved_meetings", []):
            if not isinstance(mt, dict) or not mt.get("title") or not mt.get("day") or not mt.get("time_slot"):
                continue
            conn.execute(
                """
                INSERT INTO saved_meetings (title, meeting_date, day, time_slot, department, location, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(mt["title"]).strip(),
                    mt.get("meeting_date"),
                    normalize_day(mt["day"]),
                    normalize_time_slot(mt["time_slot"]),
                    mt.get("department"),
                    mt.get("location"),
                    mt.get("notes"),
                ),
            )
            meetings_imported += 1
    except (KeyError, ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid workspace backup data: {exc}",
        ) from exc

    return {
        "restored": True,
        "mode": mode,
        "promos_processed": promos_imported,
        "slots_processed": slots_imported,
        "members_processed": members_imported,
        "shift_plans_imported": shifts_imported,
        "meetings_imported": meetings_imported,
    }


@app.delete("/api/workspace/reset", tags=["Workspace & Backup"])
def reset_workspace(
    confirm: str = Query(..., description="Must equal 'RESET' to wipe all club data."),
    conn: sqlite3.Connection = Depends(get_db),
):
    if confirm != "RESET":
        raise HTTPException(
            status_code=400,
            detail="Confirmation string must equal 'RESET' to clear the database.",
        )
    conn.execute("DELETE FROM saved_meetings;")
    conn.execute("DELETE FROM saved_shift_plans;")
    conn.execute("DELETE FROM members;")
    conn.execute("DELETE FROM schedule_slots;")
    conn.execute("DELETE FROM promos;")
    return {"reset": True, "message": "Workspace cleared completely."}
