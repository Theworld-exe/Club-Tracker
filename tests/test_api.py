"""End-to-end FastAPI integration tests using isolated temporary SQLite & .env files."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import database
import main


class TestClubTrackerAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test_club.db"
        self.env_path = Path(self.tmpdir.name) / ".env"

        self._orig_db_path = database.DB_PATH
        self._orig_env_file = main.ENV_FILE
        self._orig_env_key = os.environ.get("GEMINI_API_KEY")

        database.DB_PATH = self.db_path
        main.ENV_FILE = self.env_path
        os.environ["GEMINI_API_KEY"] = ""
        database.init_db(self.db_path)

        self.client_ctx = TestClient(main.app)
        self.client = self.client_ctx.__enter__()

    def tearDown(self) -> None:
        self.client_ctx.__exit__(None, None, None)
        database.DB_PATH = self._orig_db_path
        main.ENV_FILE = self._orig_env_file
        if self._orig_env_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._orig_env_key
        self.tmpdir.cleanup()

    def test_spa_and_settings_endpoints(self) -> None:
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("ClubPulse Operations", res.text)
        # Regression check: ensure Promo Members modal is not duplicated
        self.assertEqual(res.text.count("PROMO MEMBERS MANAGER MODAL"), 1)

        st = self.client.get("/api/settings/status")
        self.assertEqual(st.status_code, 200)
        self.assertFalse(st.json()["has_env_key"])

        # Reject newline injection in API key
        bad = self.client.post("/api/settings/api-key", json={"api_key": "abc\nINJECTED=1"})
        self.assertEqual(bad.status_code, 400)

        # Save valid placeholder key into temp .env
        saved = self.client.post("/api/settings/api-key", json={"api_key": "TEST_KEY_12345678"})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()["has_env_key"])
        self.assertEqual(saved.json()["masked_key"], "TEST...5678")

    def test_promo_slot_member_crud_and_partial_update_regression(self) -> None:
        # Create promo with slots
        p_res = self.client.post(
            "/api/promos",
            json={
                "name": "L2 Info",
                "academic_year": "2026/2027",
                "schedule_slots": [
                    {
                        "day": "Saturday",
                        "time_slot": "08:00-09:30",
                        "group_target": "ALL",
                        "session_type": "COURS",
                        "module_name": "Algo",
                        "room": "Amphi 1",
                    },
                    {
                        "day": "Saturday",
                        "time_slot": "09:30-11:00",
                        "group_target": "G1",
                        "session_type": "TP",
                        "module_name": "Archi",
                        "room": "Lab 1",
                    },
                ],
            },
        )
        self.assertEqual(p_res.status_code, 201)
        promo = p_res.json()
        promo_id = promo["id"]
        self.assertEqual(promo["slot_count"], 2)

        # Duplicate promo without merge/overwrite -> 409
        dup_res = self.client.post("/api/promos", json={"name": "l2 info"})
        self.assertEqual(dup_res.status_code, 409)

        # Merge G2 slot while keeping G1 & ALL
        merge_res = self.client.post(
            "/api/promos",
            json={
                "name": "L2 Info",
                "merge_slots": True,
                "schedule_slots": [
                    {
                        "day": "Sunday",
                        "time_slot": "08:00-09:30",
                        "group_target": "G2",
                        "session_type": "TD",
                        "module_name": "Math",
                    }
                ],
            },
        )
        self.assertEqual(merge_res.status_code, 201)
        self.assertEqual(merge_res.json()["slot_count"], 3)

        # Create member
        m_res = self.client.post(
            "/api/members",
            json={
                "full_name": "Yacine Benali",
                "department": "dev",
                "role": "dept lead",
                "promo_id": promo_id,
                "group_name": "1",
                "discord": "@yacine",
            },
        )
        self.assertEqual(m_res.status_code, 201)
        member = m_res.json()
        member_id = member["id"]
        self.assertEqual(member["group_name"], "G1")
        self.assertEqual(member["matching_slots_count"], 2)

        # Regression test: partial PUT /api/members/{id} without is_active or shift_exempt
        upd_group = self.client.put(
            f"/api/members/{member_id}",
            json={"group_name": "2"},
        )
        self.assertEqual(upd_group.status_code, 200)
        self.assertEqual(upd_group.json()["group_name"], "G2")
        self.assertTrue(upd_group.json()["is_active"])
        self.assertFalse(upd_group.json()["shift_exempt"])

        # Partial PUT toggling only shift_exempt
        upd_exempt = self.client.put(
            f"/api/members/{member_id}",
            json={"shift_exempt": True},
        )
        self.assertEqual(upd_exempt.status_code, 200)
        self.assertTrue(upd_exempt.json()["shift_exempt"])
        self.assertTrue(upd_exempt.json()["is_active"])

        # Explicit null on required field preserves existing value instead of HTTP 500
        upd_null = self.client.put(
            f"/api/members/{member_id}",
            json={"full_name": None},
        )
        self.assertEqual(upd_null.status_code, 200)
        self.assertEqual(upd_null.json()["full_name"], "Yacine Benali")

        # Bulk assign with duplicate IDs
        assign_res = self.client.post(
            f"/api/promos/{promo_id}/members/assign",
            json={"member_ids": [member_id, member_id], "group_name": "G1"},
        )
        self.assertEqual(assign_res.status_code, 200)
        self.assertEqual(assign_res.json()["assigned_count"], 1)

    def test_csv_import_export_and_analysis_endpoints(self) -> None:
        csv_body = (
            b"full_name,department,role,promo_name,group_name,discord,is_active,shift_exempt\n"
            b"Amine K,Dev,Member,L3 GL,1,@amine,true,false\n"
            b"Sara M,Design,Dept Lead,L3 GL,2,@sara,true,false\n"
            b"Karim B,Media,Bureau,L3 GL,3,@karim,true,true\n"
        )

        imp = self.client.post(
            "/api/members/bulk-csv?on_duplicate=update",
            files={"file": ("roster.csv", csv_body, "text/csv")},
        )
        self.assertEqual(imp.status_code, 200)
        data = imp.json()
        self.assertEqual(data["imported_count"], 3)
        self.assertEqual(data["created_promos"], ["L3 GL"])

        # Re-importing same CSV updates existing rows cleanly (no duplicates)
        imp2 = self.client.post(
            "/api/members/bulk-csv?on_duplicate=update",
            files={"file": ("roster.csv", csv_body, "text/csv")},
        )
        self.assertEqual(imp2.status_code, 200)
        self.assertEqual(imp2.json()["updated_count"], 3)
        self.assertEqual(imp2.json()["imported_count"], 0)

        # Export CSV
        exp = self.client.get("/api/members/export-csv")
        self.assertEqual(exp.status_code, 200)
        self.assertIn("Amine K", exp.text)

        # Slot breakdown & Heatmap
        slot_bd = self.client.get("/api/analysis/slot?day=Monday&time_slot=08:00-09:30")
        self.assertEqual(slot_bd.status_code, 200)
        self.assertEqual(slot_bd.json()["strict_free_count"], 3)

        hm = self.client.get("/api/analysis/heatmap")
        self.assertEqual(hm.status_code, 200)
        self.assertEqual(len(hm.json()["cells"]), 36)

        # Shift generation + save + retrieve + delete
        shifts = self.client.post(
            "/api/analysis/shifts",
            json={
                "day": "Monday",
                "time_slots": ["08:00-09:30", "09:30-11:00"],
                "members_per_shift": 2,
            },
        )
        self.assertEqual(shifts.status_code, 200)
        sched = shifts.json()
        self.assertEqual(sched["excluded_exempt_count"], 1)

        plan_res = self.client.post(
            "/api/shift-plans",
            json={"title": "Open Day Stand", "event_date": "2026-11-25", "schedule": sched},
        )
        self.assertEqual(plan_res.status_code, 201)
        plan_id = plan_res.json()["id"]
        self.assertEqual(len(self.client.get("/api/shift-plans").json()), 1)
        self.assertEqual(self.client.delete(f"/api/shift-plans/{plan_id}").status_code, 200)

        # Meeting creation + invalid promo_id returns 404 (not 500)
        bad_mt = self.client.post(
            "/api/meetings",
            json={"title": "Invalid Promo Sync", "day": "Monday", "time_slot": "08:00-09:30", "promo_id": 9999},
        )
        self.assertEqual(bad_mt.status_code, 404)

        mt_res = self.client.post(
            "/api/meetings",
            json={"title": "Weekly Sync", "day": "Monday", "time_slot": "08:00-09:30", "location": "Local"},
        )
        self.assertEqual(mt_res.status_code, 201)
        self.assertEqual(mt_res.json()["expected_free_count"], 3)

    def test_workspace_backup_restore_and_reset(self) -> None:
        # Seed a promo and member
        p = self.client.post(
            "/api/promos",
            json={"name": "M1 Cyber", "schedule_slots": []},
        ).json()
        self.client.post(
            "/api/members",
            json={
                "full_name": "Nadia H",
                "department": "HR",
                "promo_id": p["id"],
                "group_name": "G1",
            },
        )

        # Export JSON snapshot & SQLite binary backup
        snap_res = self.client.get("/api/workspace/export")
        self.assertEqual(snap_res.status_code, 200)
        snapshot_bytes = snap_res.content

        db_res = self.client.get("/api/workspace/export-db")
        self.assertEqual(db_res.status_code, 200)
        self.assertTrue(db_res.content.startswith(b"SQLite format 3\x00"))

        # Reset requires confirm=RESET
        bad_reset = self.client.delete("/api/workspace/reset?confirm=NOPE")
        self.assertEqual(bad_reset.status_code, 400)

        ok_reset = self.client.delete("/api/workspace/reset?confirm=RESET")
        self.assertEqual(ok_reset.status_code, 200)
        self.assertEqual(self.client.get("/api/promos").json(), [])

        # Restore JSON snapshot
        imp = self.client.post(
            "/api/workspace/import?mode=replace",
            files={"file": ("backup.json", snapshot_bytes, "application/json")},
        )
        self.assertEqual(imp.status_code, 200)
        self.assertEqual(imp.json()["promos_processed"], 1)
        self.assertEqual(imp.json()["members_processed"], 1)

        # Malformed workspace snapshot returns 400 cleanly
        corrupt = json.dumps(
            {
                "promos": [
                    {
                        "name": "BadPromo",
                        "schedule_slots": [{"day": "InvalidDay", "time_slot": "08:00-09:30", "session_type": "TD"}],
                    }
                ]
            }
        ).encode("utf-8")
        bad_imp = self.client.post(
            "/api/workspace/import?mode=merge",
            files={"file": ("bad.json", corrupt, "application/json")},
        )
        self.assertEqual(bad_imp.status_code, 400)

    def test_scan_endpoint_validation_and_secret_redaction(self) -> None:
        # Empty upload -> 400
        empty = self.client.post(
            "/api/promos/scan",
            files={"file": ("empty.pdf", b"", "application/pdf")},
        )
        self.assertEqual(empty.status_code, 400)

        # Unsupported file type -> 415
        bad_mime = self.client.post(
            "/api/promos/scan",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            headers={"X-Gemini-Api-Key": "SECRET_TEST_KEY"},
        )
        self.assertEqual(bad_mime.status_code, 415)

        # Oversized upload -> 413
        with patch.object(main, "MAX_TIMETABLE_UPLOAD_BYTES", 8):
            big = self.client.post(
                "/api/promos/scan",
                files={"file": ("big.pdf", b"%PDF-1.4 oversized", "application/pdf")},
                headers={"X-Gemini-Api-Key": "SECRET_TEST_KEY"},
            )
            self.assertEqual(big.status_code, 413)

        # Secret redaction if upstream error contains API key
        with patch("main.parse_timetable_bytes", side_effect=RuntimeError("Upstream failed with SECRET_LEAK_XYZ")):
            err = self.client.post(
                "/api/promos/scan",
                files={"file": ("tt.pdf", b"%PDF-1.4", "application/pdf")},
                headers={"X-Gemini-Api-Key": "SECRET_LEAK_XYZ"},
            )
            self.assertEqual(err.status_code, 502)
            self.assertNotIn("SECRET_LEAK_XYZ", err.text)
            self.assertIn("[REDACTED]", err.text)


if __name__ == "__main__":
    unittest.main()
