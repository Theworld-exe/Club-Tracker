"""Analytical engine for slot breakdowns, weekly availability heatmaps, and shift rotations."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from models import (
    CANONICAL_DAYS,
    CANONICAL_TIME_SLOTS,
    AssignedShiftMember,
    HeatmapCell,
    HeatmapResponse,
    MemberShiftSummary,
    MemberSlotItem,
    MemberStatusType,
    ShiftScheduleResponse,
    ShiftSlotAssignment,
    SlotBreakdownResponse,
    normalize_day,
    normalize_time_slot,
)

# Priority order when multiple schedule_slots match a member in the same cell:
# Strictly TP > TD > COURS
SESSION_PRIORITY: dict[str, int] = {
    "TP": 3,
    "TD": 2,
    "COURS": 1,
}

STATUS_WEIGHTS: dict[str, float] = {
    "FREE": 1.0,
    "COURS": 0.5,
    "TD": 0.0,
    "TP": 0.0,
}

_DAY_INDEX: dict[str, int] = {d: i for i, d in enumerate(CANONICAL_DAYS)}
_SLOT_INDEX: dict[str, int] = {s: i for i, s in enumerate(CANONICAL_TIME_SLOTS)}


def _fetch_filtered_members(
    conn: sqlite3.Connection,
    department: str | None = None,
    promo_id: int | None = None,
    include_inactive: bool = False,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []

    if not include_inactive:
        clauses.append("m.is_active = 1")

    if department and department.strip() and department.strip().upper() != "ALL":
        clauses.append("LOWER(m.department) = LOWER(?)")
        params.append(department.strip())

    if promo_id is not None:
        clauses.append("m.promo_id = ?")
        params.append(promo_id)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT
            m.id, m.full_name, m.department, m.role, m.promo_id,
            p.name AS promo_name, m.group_name, m.discord, m.phone,
            m.is_active, m.shift_exempt
        FROM members m
        JOIN promos p ON p.id = m.promo_id
        {where_sql}
        ORDER BY m.full_name COLLATE NOCASE ASC
        """,
        params,
    ).fetchall()


def _resolve_member_in_slot(
    member: sqlite3.Row,
    matching_slots: list[sqlite3.Row],
) -> MemberSlotItem:
    """Resolve a member's status for a single (day, time_slot) with strict TP > TD > COURS priority."""
    base_kwargs = {
        "id": int(member["id"]),
        "full_name": member["full_name"],
        "department": member["department"],
        "role": member["role"] or "Member",
        "promo_id": int(member["promo_id"]),
        "promo_name": member["promo_name"],
        "group_name": member["group_name"],
        "discord": member["discord"],
        "phone": member["phone"],
        "is_active": bool(member["is_active"]),
        "shift_exempt": bool(member["shift_exempt"]),
    }

    if not matching_slots:
        return MemberSlotItem(
            **base_kwargs,
            status="FREE",
            weight=STATUS_WEIGHTS["FREE"],
            module_name=None,
            room=None,
            overlap_note=None,
        )

    sorted_matches = sorted(
        matching_slots,
        key=lambda r: (
            SESSION_PRIORITY.get(r["session_type"], 0),
            0 if r["group_target"] == "ALL" else 1,
        ),
        reverse=True,
    )
    top = sorted_matches[0]
    chosen_status: MemberStatusType = top["session_type"]

    same_priority = [r for r in sorted_matches if r["session_type"] == chosen_status]
    modules = list(
        dict.fromkeys(r["module_name"] for r in same_priority if r["module_name"])
    )
    rooms = list(dict.fromkeys(r["room"] for r in same_priority if r["room"]))

    module_str = " / ".join(modules) if modules else None
    room_str = " / ".join(rooms) if rooms else None

    overlap_note = None
    if len(sorted_matches) > 1:
        others = [
            f"{r['session_type']}{' ' + r['module_name'] if r['module_name'] else ''}"
            for r in sorted_matches[1:]
        ]
        overlap_note = (
            f"Overlap resolved ({chosen_status} prioritized over {', '.join(others)})"
        )

    return MemberSlotItem(
        **base_kwargs,
        status=chosen_status,
        weight=STATUS_WEIGHTS[chosen_status],
        module_name=module_str,
        room=room_str,
        overlap_note=overlap_note,
    )


def get_slot_breakdown(
    conn: sqlite3.Connection,
    day: str,
    time_slot: str,
    department: str | None = None,
    promo_id: int | None = None,
    include_inactive: bool = False,
) -> SlotBreakdownResponse:
    """Return FREE, COURS, TD, and TP member lists and weighted stats for a specific (day, time_slot)."""
    norm_day = normalize_day(day)
    norm_slot = normalize_time_slot(time_slot)
    clean_dept = (
        department.strip()
        if department and department.strip() and department.strip().upper() != "ALL"
        else None
    )

    members = _fetch_filtered_members(
        conn,
        department=clean_dept,
        promo_id=promo_id,
        include_inactive=include_inactive,
    )
    buckets: dict[str, list[MemberSlotItem]] = {
        "FREE": [],
        "COURS": [],
        "TD": [],
        "TP": [],
    }

    if members:
        promo_ids = sorted({int(m["promo_id"]) for m in members})
        placeholders = ",".join("?" for _ in promo_ids)
        slot_rows = conn.execute(
            f"""
            SELECT promo_id, group_target, session_type, module_name, room
            FROM schedule_slots
            WHERE day = ? AND time_slot = ? AND promo_id IN ({placeholders})
            """,
            [norm_day, norm_slot, *promo_ids],
        ).fetchall()

        by_promo_group: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
        for s_row in slot_rows:
            by_promo_group[(int(s_row["promo_id"]), s_row["group_target"])].append(
                s_row
            )

        for m in members:
            pid = int(m["promo_id"])
            grp = m["group_name"]
            matched = (
                by_promo_group.get((pid, "ALL"), [])
                + by_promo_group.get((pid, grp), [])
            )
            resolved = _resolve_member_in_slot(m, matched)
            buckets[resolved.status].append(resolved)

    total = len(members)
    free_cnt = len(buckets["FREE"])
    cours_cnt = len(buckets["COURS"])
    td_cnt = len(buckets["TD"])
    tp_cnt = len(buckets["TP"])
    flex_cnt = free_cnt + cours_cnt
    weighted_pts = free_cnt * 1.0 + cours_cnt * 0.5

    strict_pct = round((free_cnt / total) * 100.0, 1) if total > 0 else 0.0
    weighted_pct = round((weighted_pts / total) * 100.0, 1) if total > 0 else 0.0

    return SlotBreakdownResponse(
        day=norm_day,  # type: ignore[arg-type]
        time_slot=norm_slot,  # type: ignore[arg-type]
        department=clean_dept,
        promo_id=promo_id,
        total_members=total,
        counts={
            "FREE": free_cnt,
            "COURS": cours_cnt,
            "TD": td_cnt,
            "TP": tp_cnt,
        },
        strict_free_count=free_cnt,
        flexible_free_count=flex_cnt,
        strict_percentage=strict_pct,
        weighted_score=weighted_pct,
        FREE=buckets["FREE"],
        COURS=buckets["COURS"],
        TD=buckets["TD"],
        TP=buckets["TP"],
    )


def generate_weekly_heatmap(
    conn: sqlite3.Connection,
    department: str | None = None,
    promo_id: int | None = None,
    include_inactive: bool = False,
) -> HeatmapResponse:
    """Compute strict and weighted availability across all 6 days x 6 daily slots in one batch."""
    clean_dept = (
        department.strip()
        if department and department.strip() and department.strip().upper() != "ALL"
        else None
    )
    members = _fetch_filtered_members(
        conn,
        department=clean_dept,
        promo_id=promo_id,
        include_inactive=include_inactive,
    )
    total = len(members)

    by_cell_promo_group: dict[tuple[str, str, int, str], list[sqlite3.Row]] = defaultdict(list)
    if members:
        promo_ids = sorted({int(m["promo_id"]) for m in members})
        placeholders = ",".join("?" for _ in promo_ids)
        all_slots = conn.execute(
            f"""
            SELECT promo_id, day, time_slot, group_target, session_type, module_name, room
            FROM schedule_slots
            WHERE promo_id IN ({placeholders})
            """,
            promo_ids,
        ).fetchall()
        for s_row in all_slots:
            by_cell_promo_group[
                (
                    s_row["day"],
                    s_row["time_slot"],
                    int(s_row["promo_id"]),
                    s_row["group_target"],
                )
            ].append(s_row)

    cells: list[HeatmapCell] = []
    for day in CANONICAL_DAYS:
        for time_slot in CANONICAL_TIME_SLOTS:
            free_cnt = 0
            cours_cnt = 0
            td_cnt = 0
            tp_cnt = 0

            for m in members:
                pid = int(m["promo_id"])
                grp = m["group_name"]
                matched = (
                    by_cell_promo_group.get((day, time_slot, pid, "ALL"), [])
                    + by_cell_promo_group.get((day, time_slot, pid, grp), [])
                )
                if not matched:
                    free_cnt += 1
                else:
                    best_type = max(
                        (r["session_type"] for r in matched),
                        key=lambda t: SESSION_PRIORITY.get(t, 0),
                    )
                    if best_type == "TP":
                        tp_cnt += 1
                    elif best_type == "TD":
                        td_cnt += 1
                    else:
                        cours_cnt += 1

            flex_cnt = free_cnt + cours_cnt
            weighted_pts = round(free_cnt * 1.0 + cours_cnt * 0.5, 2)
            strict_pct = round((free_cnt / total) * 100.0, 1) if total > 0 else 0.0
            weighted_pct = round((weighted_pts / total) * 100.0, 1) if total > 0 else 0.0

            cells.append(
                HeatmapCell(
                    day=day,  # type: ignore[arg-type]
                    time_slot=time_slot,  # type: ignore[arg-type]
                    total_members=total,
                    strict_free_count=free_cnt,
                    cours_count=cours_cnt,
                    td_count=td_cnt,
                    tp_count=tp_cnt,
                    flexible_free_count=flex_cnt,
                    strict_percentage=strict_pct,
                    weighted_points=weighted_pts,
                    weighted_score=weighted_pct,
                )
            )

    top_recommended = sorted(
        cells,
        key=lambda c: (
            -c.weighted_score,
            -c.strict_free_count,
            -c.flexible_free_count,
            _DAY_INDEX[c.day],
            _SLOT_INDEX[c.time_slot],
        ),
    )[:5]

    top_strict = sorted(
        cells,
        key=lambda c: (
            -c.strict_percentage,
            -c.weighted_score,
            _DAY_INDEX[c.day],
            _SLOT_INDEX[c.time_slot],
        ),
    )[:5]

    return HeatmapResponse(
        department=clean_dept,
        promo_id=promo_id,
        total_members=total,
        days=list(CANONICAL_DAYS),
        time_slots=list(CANONICAL_TIME_SLOTS),
        cells=cells,
        top_recommended_slots=top_recommended,
        top_strict_slots=top_strict,
    )


def generate_exhibition_shifts(
    conn: sqlite3.Connection,
    day: str,
    time_slots: list[str],
    members_per_shift: int,
    department: str | None = None,
    promo_id: int | None = None,
    allow_cours_fallback: bool = True,
    include_shift_exempt: bool = False,
    excluded_member_ids: list[int] | None = None,
) -> ShiftScheduleResponse:
    """Build balanced stand/exhibition shifts respecting shift exemptions, active status, and max-2 consecutive guard."""
    norm_day = normalize_day(day)
    clean_dept = (
        department.strip()
        if department and department.strip() and department.strip().upper() != "ALL"
        else None
    )
    excluded_set = set(excluded_member_ids or [])

    ordered_slots = sorted(
        {normalize_time_slot(s) for s in time_slots},
        key=lambda s: _SLOT_INDEX[s],
    )

    assigned_shifts_count: dict[int, int] = defaultdict(int)
    assigned_free_count: dict[int, int] = defaultdict(int)
    assigned_cours_count: dict[int, int] = defaultdict(int)
    assigned_slot_indices: dict[int, set[int]] = defaultdict(set)
    assigned_slot_labels: dict[int, list[str]] = defaultdict(list)
    member_directory: dict[int, MemberSlotItem] = {}
    exempt_seen_ids: set[int] = set()

    shifts_output: list[ShiftSlotAssignment] = []
    unfilled_total = 0

    for slot_str in ordered_slots:
        slot_idx = _SLOT_INDEX[slot_str]
        breakdown = get_slot_breakdown(
            conn=conn,
            day=norm_day,
            time_slot=slot_str,
            department=clean_dept,
            promo_id=promo_id,
            include_inactive=False,
        )

        for item in breakdown.FREE + breakdown.COURS + breakdown.TD + breakdown.TP:
            member_directory[item.id] = item
            if (item.shift_exempt and not include_shift_exempt) or (item.id in excluded_set):
                exempt_seen_ids.add(item.id)

        def _is_eligible(m: MemberSlotItem) -> bool:
            if m.id in excluded_set:
                return False
            if m.shift_exempt and not include_shift_exempt:
                return False
            return True

        def _would_exceed_two_consecutive(member_id: int, current_idx: int = slot_idx) -> bool:
            prev_indices = assigned_slot_indices[member_id]
            return (current_idx - 1 in prev_indices) and (current_idx - 2 in prev_indices)

        def _candidate_sort_key(
            m: MemberSlotItem, current_idx: int = slot_idx
        ) -> tuple[int, int, int, str]:
            exceeds_consec = 1 if _would_exceed_two_consecutive(m.id, current_idx) else 0
            shifts_so_far = assigned_shifts_count[m.id]
            worked_prev = 1 if (current_idx - 1 in assigned_slot_indices[m.id]) else 0
            return (exceeds_consec, shifts_so_far, worked_prev, m.full_name.lower())

        eligible_free = [m for m in breakdown.FREE if _is_eligible(m)]
        eligible_cours = [m for m in breakdown.COURS if _is_eligible(m)]

        free_candidates = sorted(eligible_free, key=_candidate_sort_key)
        cours_candidates = sorted(eligible_cours, key=_candidate_sort_key)

        selected: list[tuple[MemberSlotItem, str]] = []

        for m in free_candidates:
            if len(selected) >= members_per_shift:
                break
            selected.append((m, "100% Free"))

        if len(selected) < members_per_shift and allow_cours_fallback:
            selected_ids = {m.id for m, _ in selected}
            for m in cours_candidates:
                if len(selected) >= members_per_shift:
                    break
                if m.id in selected_ids:
                    continue
                mod_label = m.module_name or "Lecture"
                selected.append((m, f"In Cours: {mod_label}"))

        slot_assigned_models: list[AssignedShiftMember] = []
        for m, note in selected:
            assigned_shifts_count[m.id] += 1
            if m.status == "FREE":
                assigned_free_count[m.id] += 1
            else:
                assigned_cours_count[m.id] += 1
            assigned_slot_indices[m.id].add(slot_idx)
            assigned_slot_labels[m.id].append(slot_str)

            slot_assigned_models.append(
                AssignedShiftMember(
                    id=m.id,
                    full_name=m.full_name,
                    department=m.department,
                    role=m.role,
                    promo_id=m.promo_id,
                    promo_name=m.promo_name,
                    group_name=m.group_name,
                    discord=m.discord,
                    phone=m.phone,
                    availability_status="FREE" if m.status == "FREE" else "COURS",
                    status_note=note,
                    module_name=m.module_name,
                    room=m.room,
                    shifts_assigned_today=assigned_shifts_count[m.id],
                )
            )

        shortfall = max(0, members_per_shift - len(slot_assigned_models))
        unfilled_total += shortfall

        shifts_output.append(
            ShiftSlotAssignment(
                time_slot=slot_str,  # type: ignore[arg-type]
                requested_count=members_per_shift,
                filled_count=len(slot_assigned_models),
                shortfall=shortfall,
                free_pool_count=len(eligible_free),
                cours_pool_count=len(eligible_cours),
                assigned_members=slot_assigned_models,
            )
        )

    summaries: list[MemberShiftSummary] = []
    for mid, total_cnt in assigned_shifts_count.items():
        info = member_directory[mid]
        summaries.append(
            MemberShiftSummary(
                id=mid,
                full_name=info.full_name,
                department=info.department,
                role=info.role,
                promo_name=info.promo_name,
                group_name=info.group_name,
                discord=info.discord,
                phone=info.phone,
                total_shifts=total_cnt,
                free_shifts=assigned_free_count[mid],
                cours_shifts=assigned_cours_count[mid],
                assigned_slots=assigned_slot_labels[mid],
            )
        )

    summaries.sort(key=lambda s: (-s.total_shifts, s.full_name.lower()))

    return ShiftScheduleResponse(
        day=norm_day,  # type: ignore[arg-type]
        department=clean_dept,
        promo_id=promo_id,
        members_per_shift=members_per_shift,
        allow_cours_fallback=allow_cours_fallback,
        excluded_exempt_count=len(exempt_seen_ids),
        shifts=shifts_output,
        member_summaries=summaries,
        unfilled_spots_total=unfilled_total,
    )
