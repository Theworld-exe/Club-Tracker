"""Unit tests for Gemini Vision timetable parser normalization and mock extraction."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from models import ExtractedSlot, MultiTimetableExtraction, TimetableExtraction
from parser import (
    DEFAULT_GEMINI_MODEL,
    _split_promo_and_inferred_group,
    expand_group_targets,
    get_active_gemini_model,
    normalize_extraction,
    parse_timetable_bytes,
    resolve_mime_type,
)


class TestParserHelpers(unittest.TestCase):
    def test_resolve_mime_type(self) -> None:
        self.assertEqual(resolve_mime_type("application/pdf", "sched.pdf"), "application/pdf")
        self.assertEqual(resolve_mime_type("image/jpg", "photo.jpg"), "image/jpeg")
        self.assertEqual(resolve_mime_type("application/octet-stream", "scan.webp"), "image/webp")
        with self.assertRaises(ValueError):
            resolve_mime_type("text/plain", "notes.txt")

    def test_split_promo_and_inferred_group(self) -> None:
        self.assertEqual(_split_promo_and_inferred_group("Timetable — L2_S2-G6"), ("L2_S2", "G6"))
        self.assertEqual(_split_promo_and_inferred_group("L1 Info Sec 2 - G03"), ("L1 Info Sec 2", "G3"))
        self.assertEqual(
            _split_promo_and_inferred_group("", fallback_filename="L3_GL_G2.pdf"),
            ("L3_GL", "G2"),
        )

    def test_expand_group_targets(self) -> None:
        self.assertEqual(expand_group_targets("G1/G2", "TD"), ["G1", "G2"])
        self.assertEqual(expand_group_targets("G3-G5", "TP"), ["G3", "G4", "G5"])
        self.assertEqual(expand_group_targets("ALL", "COURS"), ["ALL"])
        self.assertEqual(expand_group_targets("ALL", "TD", default_td_tp_group="G6"), ["G6"])

    def test_normalize_extraction_splits_and_deduplicates(self) -> None:
        raw = TimetableExtraction(
            promo_name="Timetable — L2_S2-G6",
            slots=[
                ExtractedSlot(
                    day="Monday",
                    time_slot="08:00-09:30",
                    group_target="G6",
                    session_type="COURS",
                    module_name="Algo",
                    room="Amphi 1",
                ),
                ExtractedSlot(
                    day="Monday",
                    time_slot="09:30-11:00",
                    group_target="G1/G2",
                    session_type="TD",
                    module_name="Math",
                    room="S12",
                ),
                # Duplicate of the first G1 TD slot after expansion
                ExtractedSlot(
                    day="Monday",
                    time_slot="09:30-11:00",
                    group_target="G1",
                    session_type="TD",
                    module_name="Math",
                    room="S12",
                ),
            ],
        )
        normalized = normalize_extraction(raw)
        self.assertEqual(normalized.promo_name, "L2_S2")
        # COURS forced to ALL; G1/G2 split into G1 & G2 and deduplicated -> 3 slots total
        self.assertEqual(len(normalized.slots), 3)
        self.assertEqual(normalized.slots[0].group_target, "ALL")
        self.assertEqual([s.group_target for s in normalized.slots[1:]], ["G1", "G2"])

    def test_get_active_gemini_model_upgrades_deprecated(self) -> None:
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-2.5-flash"}, clear=False):
            self.assertEqual(get_active_gemini_model(), DEFAULT_GEMINI_MODEL)
        with patch.dict(os.environ, {"GEMINI_MODEL": "models/gemini-3.8-flash"}, clear=False):
            self.assertEqual(get_active_gemini_model(), "gemini-3.8-flash")

    def test_parse_timetable_bytes_with_mock_client(self) -> None:
        mock_payload = MultiTimetableExtraction(
            timetables=[
                TimetableExtraction(
                    promo_name="L1 Info Sec 1",
                    slots=[
                        ExtractedSlot(
                            day="Sunday",
                            time_slot="11:00-12:30",
                            group_target="ALL",
                            session_type="COURS",
                            module_name="Analyse",
                            room="Amphi A",
                        )
                    ],
                )
            ]
        )

        class MockModels:
            def generate_content(self, **_kwargs):
                return SimpleNamespace(parsed=mock_payload, text=None)

        mock_client = SimpleNamespace(models=MockModels())
        result = parse_timetable_bytes(
            file_bytes=b"%PDF-1.4 fake",
            mime_type="application/pdf",
            filename="L1_Info_Sec_1.pdf",
            client_override=mock_client,
        )
        self.assertEqual(result.promo_name, "L1 Info Sec 1")
        self.assertEqual(len(result.slots), 1)
        self.assertEqual(result.slots[0].module_name, "Analyse")

    def test_parse_timetable_bytes_rejects_empty_and_missing_key(self) -> None:
        with self.assertRaises(ValueError):
            parse_timetable_bytes(b"", "application/pdf")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            with self.assertRaises(PermissionError):
                parse_timetable_bytes(b"fake-bytes", "image/png", api_key="")


if __name__ == "__main__":
    unittest.main()
