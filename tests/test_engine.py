"""Unit tests for the analytical availability, heatmap, and shift fairness engine."""

from __future__ import annotations

import sqlite3
import unittest

from database import SCHEMA_SQL
from engine import (
    generate_exhibition_shifts,
    generate_weekly_heatmap,
    get_slot_breakdown,
)


def _create_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_SQL)
    return conn


class TestAnalyticalEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _create_memory_db()
        cur = self.conn.execute("INSERT INTO promos (name, academic_year) VALUES (?, ?)", ("L2 Info", "2026/2027"))
        self.promo_id = int(cur.lastrowid)

        # Schedule slots on Saturday 08:00-09:30:
        # - ALL has COURS (Algo)
        # - G1 also has TP (Archi) -> must resolve to TP (TP > COURS)
        # - G2 also has TD (Math)  -> must resolve to TD (TD > COURS)
        # - G3 only matches ALL    -> resolves to COURS
        # On Saturday 09:30-11:00: everyone is FREE
        self.conn.executemany(
            """
            INSERT INTO schedule_slots (promo_id, day, time_slot, group_target, session_type, module_name, room)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (self.promo_id, "Saturday", "08:00-09:30", "ALL", "COURS", "Algo", "Amphi 1"),
                (self.promo_id, "Saturday", "08:00-09:30", "G1", "TP", "Archi", "Lab 2"),
                (self.promo_id, "Saturday", "08:00-09:30", "G2", "TD", "Math", "S12"),
                (self.promo_id, "Saturday", "08:00-09:30", "G1", "TD", "LowerPriority", "S10"),
            ],
        )

        # Create another promo with zero slots on Saturday 08:00-09:30 so we have a FREE member
        cur2 = self.conn.execute("INSERT INTO promos (name) VALUES (?)", ("M1 IA",))
        self.promo2_id = int(cur2.lastrowid)

        self.conn.executemany(
            """
            INSERT INTO members (full_name, department, role, promo_id, group_name, is_active, shift_exempt)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("Alice Free", "Dev", "Member", self.promo2_id, "G1", 1, 0),
                ("Bob Cours", "Dev", "Member", self.promo_id, "G3", 1, 0),
                ("Cara TD", "Design", "Member", self.promo_id, "G2", 1, 0),
                ("Dan TP", "Dev", "Member", self.promo_id, "G1", 1, 0),
                ("Eve Inactive", "Dev", "Member", self.promo2_id, "G1", 0, 0),
                ("Frank Exempt", "Dev", "Bureau", self.promo2_id, "G1", 1, 1),
            ],
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_slot_breakdown_priority_and_weights(self) -> None:
        bd = get_slot_breakdown(self.conn, day="Saturday", time_slot="08:00-09:30")
        # Active members = 5 (Alice, Bob, Cara, Dan, Frank; Eve is inactive)
        self.assertEqual(bd.total_members, 5)
        self.assertEqual(bd.counts, {"FREE": 2, "COURS": 1, "TD": 1, "TP": 1})
        self.assertEqual(bd.strict_free_count, 2)
        self.assertEqual(bd.flexible_free_count, 3)
        self.assertEqual(bd.strict_percentage, 40.0)
        # Weighted: (2 * 1.0 + 1 * 0.5) / 5 = 50.0%
        self.assertEqual(bd.weighted_score, 50.0)

        # Verify TP > TD > COURS overlap resolution note on Dan TP
        self.assertEqual(len(bd.TP), 1)
        self.assertEqual(bd.TP[0].full_name, "Dan TP")
        self.assertIn("TP prioritized", bd.TP[0].overlap_note or "")

    def test_slot_breakdown_include_inactive_and_department_filter(self) -> None:
        bd = get_slot_breakdown(
            self.conn,
            day="Saturday",
            time_slot="08:00-09:30",
            department="Dev",
            include_inactive=True,
        )
        # Dev members: Alice, Bob, Dan, Eve (inactive), Frank = 5
        self.assertEqual(bd.total_members, 5)
        self.assertEqual(bd.counts["FREE"], 3)
        self.assertEqual(bd.counts["TD"], 0)

    def test_weekly_heatmap_dimensions_and_ranking(self) -> None:
        hm = generate_weekly_heatmap(self.conn)
        self.assertEqual(len(hm.cells), 36)  # 6 days * 6 slots
        self.assertEqual(len(hm.top_recommended_slots), 5)
        self.assertEqual(len(hm.top_strict_slots), 5)
        # Saturday 09:30-11:00 has 100% availability and should beat Saturday 08:00-09:30
        self.assertEqual(hm.top_recommended_slots[0].weighted_score, 100.0)

    def test_shift_rotation_respects_exemptions_and_consecutive_guard(self) -> None:
        # Use 4 consecutive slots where Alice, Bob, Cara, Dan are all FREE (Sunday)
        # Frank is shift_exempt -> excluded by default
        slots = ["08:00-09:30", "09:30-11:00", "11:00-12:30", "12:30-14:00"]
        res = generate_exhibition_shifts(
            self.conn,
            day="Sunday",
            time_slots=slots,
            members_per_shift=2,
            allow_cours_fallback=True,
            include_shift_exempt=False,
        )
        self.assertEqual(res.unfilled_spots_total, 0)
        self.assertEqual(res.excluded_exempt_count, 1)

        assigned_names = {
            m.full_name for sh in res.shifts for m in sh.assigned_members
        }
        self.assertNotIn("Frank Exempt", assigned_names)
        self.assertNotIn("Eve Inactive", assigned_names)

        # Fairness: 4 slots * 2 spots = 8 assignments across 4 eligible members => 2 shifts each
        shift_counts = {s.full_name: s.total_shifts for s in res.member_summaries}
        self.assertEqual(shift_counts, {"Alice Free": 2, "Bob Cours": 2, "Cara TD": 2, "Dan TP": 2})

    def test_shift_rotation_cours_fallback_toggle(self) -> None:
        # On Saturday 08:00-09:30: only Alice is FREE & non-exempt; Bob is COURS.
        res_with_cours = generate_exhibition_shifts(
            self.conn,
            day="Saturday",
            time_slots=["08:00-09:30"],
            members_per_shift=2,
            allow_cours_fallback=True,
        )
        self.assertEqual(res_with_cours.unfilled_spots_total, 0)
        statuses = [m.availability_status for m in res_with_cours.shifts[0].assigned_members]
        self.assertEqual(statuses, ["FREE", "COURS"])

        res_strict = generate_exhibition_shifts(
            self.conn,
            day="Saturday",
            time_slots=["08:00-09:30"],
            members_per_shift=2,
            allow_cours_fallback=False,
        )
        self.assertEqual(res_strict.unfilled_spots_total, 1)
        self.assertEqual(len(res_strict.shifts[0].assigned_members), 1)


if __name__ == "__main__":
    unittest.main()
