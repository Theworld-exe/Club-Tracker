# Changelog

All notable changes to **Club Tracker** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Automated unit and FastAPI integration test suite under `tests/` (`test_models.py`, `test_engine.py`, `test_parser.py`, `test_api.py`).
- GitHub Actions CI workflow (`.github/workflows/ci.yml`) running `ruff`, `pytest`, OpenAPI validation, and secret/artifact hygiene checks on Linux and Windows.
- Repository documentation and contributor standards (`.env.example`, `.gitignore`, `.editorconfig`, `.gitattributes`, `LICENSE`, `pyproject.toml`, `requirements-dev.txt`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue & PR templates).
- Upload size limits on timetable scan (25 MB) and CSV/JSON import (10 MB) endpoints.
- Graceful fallback to `models.generate_content` in `test_gemini_api_key` when the Interactions API is unavailable.

### Changed
- `build_desktop.py` now uses a portable relative `--add-data` path and copies `.env.example` instead of local `.env` by default (opt-in `--include-env` flag for private local builds only).
- `ClubTracker.spec` updated to use a portable relative `static` path instead of a machine-specific path.
- `main.py` reuses `APP_DIR` / `BUNDLE_DIR` from `database.py` and creates SQLite parent directories automatically when using custom `CLUB_DB_PATH`.
- Settings modal in `static/index.html` dynamically displays the active Gemini model reported by `/api/settings/status`.

### Fixed
- Fixed critical `KeyError` bug in `PUT /api/members/{member_id}` caused by operator precedence on `is_active` and `shift_exempt` during partial member updates (e.g., quick flag toggles and group edits).
- Fixed explicit JSON `null` values in `PUT` endpoints causing `sqlite3.IntegrityError` (HTTP 500) on required fields.
- Fixed unhandled `sqlite3.IntegrityError` (HTTP 500) when saving meetings or shift plans referencing a non-existent `promo_id` (now returns HTTP 404).
- Fixed unhandled validation exceptions during `POST /api/workspace/import` (now returns HTTP 400 and rolls back cleanly).
- Removed 4 accidental duplicate copies of the Promo Members Manager modal in `static/index.html` (trimming ~1,236 lines of duplicated DOM markup and preventing stacked overlays).
- Fixed stale member list inside the Promo Members Manager modal after editing a member profile.
- Hardened `POST /api/settings/api-key` against newline injection into `.env` and added secret redaction in Gemini error responses.

## [2.0.0] - 2026-11-06

### Added
- FastAPI backend (`main.py`), SQLite schema & additive migrations (`database.py`), Pydantic v2 domain models (`models.py`), availability & fairness engine (`engine.py`), and Gemini 3.8 Flash vision parser (`parser.py`).
- Single-page Tailwind CSS + Alpine.js dashboard (`static/index.html`) with Live Radar, Weekly Heatmap & Saved Meetings, Exhibition Shift Planner with Saved Plans, Batch AI Timetable Scanner with Visual Grid preview, Promo Members Manager, and CSV Member Directory.
- Native desktop launcher (`desktop.py`, `ClubTracker.bat`) using `pywebview` (Microsoft Edge WebView2) with browser fallback, plus PyInstaller build script (`build_desktop.py`).
- Portable workspace backup/restore (JSON snapshot, SQLite `.db` binary backup, CSV roster export, and confirmed factory reset).
