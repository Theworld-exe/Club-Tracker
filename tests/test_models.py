"""Unit tests for Pydantic schemas and domain normalizers in models.py."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from models import (
    MemberCreate,
    PromoCreate,
    ScheduleSlotCreate,
    ShiftRequest,
    normalize_day,
    normalize_department,
    normalize_group_code,
    normalize_role,
    normalize_session_type,
    normalize_time_slot,
)


class TestNormalizers(unittest.TestCase):
    def test_normalize_day_multilingual(self) -> None:
        self.assertEqual(normalize_day("Samedi"), "Saturday")
        self.assertEqual(normalize_day("dim."), "Sunday")
        self.assertEqual(normalize_day("Lundi"), "Monday")
        self.assertEqual(normalize_day("الثلاثاء"), "Tuesday")
        self.assertEqual(normalize_day("mercredi"), "Wednesday")
        self.assertEqual(normalize_day("Jeudi"), "Thursday")
        with self.assertRaises(ValueError):
            normalize_day("Friday")

    def test_normalize_time_slot_exact_and_variants(self) -> None:
        self.assertEqual(normalize_time_slot("08:00-09:30"), "08:00-09:30")
        self.assertEqual(normalize_time_slot("8h00 - 9h30"), "08:00-09:30")
        self.assertEqual(normalize_time_slot("14h à 15h30"), "14:00-15:30")
        self.assertEqual(normalize_time_slot("09:40-11:10", snap_nearest=True), "09:30-11:00")
        with self.assertRaises(ValueError):
            normalize_time_slot("09:40-11:10", snap_nearest=False)

    def test_normalize_group_code(self) -> None:
        self.assertEqual(normalize_group_code("15"), "G15")
        self.assertEqual(normalize_group_code("g06"), "G6")
        self.assertEqual(normalize_group_code("Groupe 03"), "G3")
        self.assertEqual(normalize_group_code("ALL", allow_all=True), "ALL")
        self.assertEqual(normalize_group_code("Toute la section", allow_all=True), "ALL")
        with self.assertRaises(ValueError):
            normalize_group_code("ALL", allow_all=False)
        with self.assertRaises(ValueError):
            normalize_group_code("   ")

    def test_normalize_session_type(self) -> None:
        self.assertEqual(normalize_session_type("CM"), "COURS")
        self.assertEqual(normalize_session_type("Amphi"), "COURS")
        self.assertEqual(normalize_session_type("Travaux Dirigés"), "TD")
        self.assertEqual(normalize_session_type("Labo"), "TP")
        with self.assertRaises(ValueError):
            normalize_session_type("EXAM")

    def test_normalize_department_and_role(self) -> None:
        self.assertEqual(normalize_department("dev"), "Dev")
        self.assertEqual(normalize_department("  Robotics  Club "), "Robotics Club")
        self.assertEqual(normalize_role(None), "Member")
        self.assertEqual(normalize_role("dept lead"), "Dept Lead")
        self.assertEqual(normalize_role("Treasurer"), "Treasurer")


class TestSchemas(unittest.TestCase):
    def test_schedule_slot_create_normalizes_fields(self) -> None:
        slot = ScheduleSlotCreate(
            day="lundi",
            time_slot="8h00-9h30",
            group_target="groupe 02",
            session_type="labo",
            module_name="  Algo   1 ",
            room="  Labo 3 ",
        )
        self.assertEqual(slot.day, "Monday")
        self.assertEqual(slot.time_slot, "08:00-09:30")
        self.assertEqual(slot.group_target, "G2")
        self.assertEqual(slot.session_type, "TP")
        self.assertEqual(slot.module_name, "Algo 1")
        self.assertEqual(slot.room, "Labo 3")

    def test_promo_create_accepts_aliases(self) -> None:
        promo = PromoCreate.model_validate(
            {
                "promo_name": "  L2   Info ",
                "slots": [
                    {
                        "day": "Saturday",
                        "time_slot": "08:00-09:30",
                        "group_target": "ALL",
                        "session_type": "COURS",
                    }
                ],
            }
        )
        self.assertEqual(promo.name, "L2 Info")
        self.assertEqual(len(promo.schedule_slots), 1)

    def test_member_create_rejects_all_group(self) -> None:
        with self.assertRaises(ValidationError):
            MemberCreate(
                full_name="Test Student",
                department="Dev",
                promo_id=1,
                group_name="ALL",
            )

    def test_shift_request_sorts_and_deduplicates_slots(self) -> None:
        req = ShiftRequest(
            day="dimanche",
            time_slots=["14:00-15:30", "09:30-11:00", "14:00-15:30"],
            members_per_shift=2,
        )
        self.assertEqual(req.day, "Sunday")
        self.assertEqual(req.time_slots, ["09:30-11:00", "14:00-15:30"])


if __name__ == "__main__":
    unittest.main()
