# Contributing to Club Tracker

Thank you for your interest in contributing to **Club Tracker (ClubPulse Operations)**!

## Development Setup

1. **Prerequisites**
   - Python **3.11+**
   - Git

2. **Create a virtual environment and install dependencies**
   ```bash
   python -m venv .venv
   # Windows PowerShell
   .\.venv\Scripts\Activate.ps1
   # macOS / Linux
   source .venv/bin/activate

   pip install -r requirements.txt -r requirements-dev.txt
   ```

3. **Configure local environment**
   ```bash
   cp .env.example .env
   ```
   Set `GEMINI_API_KEY` in `.env` if you plan to test live AI Vision timetable scanning. Automated tests mock Gemini API calls and do not require an API key.

4. **Run the development server**
   ```bash
   uvicorn main:app --host 127.0.0.1 --port 8000 --reload
   ```
   Open `http://127.0.0.1:8000` (and API docs at `http://127.0.0.1:8000/docs`).

## Running Checks Before Submitting a PR

Please run the linter and full test suite before opening a pull request:

```bash
ruff check .
pytest -v
```

You can also run the test suite with Python's standard library test runner:

```bash
python -m unittest discover -s tests -v
```

## Coding Guidelines

- **Python**: Follow PEP 8, type-annotate new functions, and keep `ruff check .` clean.
- **Database**: Keep SQLite schema changes additive via `_MIGRATIONS` in [database.py](database.py) so existing local `club_scheduler.db` files upgrade seamlessly. Always use parameterized queries (`?`).
- **Validation**: Normalize domain inputs (`day`, `time_slot`, `group_target`, `session_type`, `department`, `role`) through helpers in [models.py](models.py).
- **Frontend**: Keep [static/index.html](static/index.html) build-free (Tailwind CSS + Alpine.js). Always bind dynamic text via `x-text` rather than `x-html` to prevent XSS.
- **Security**: Never commit `.env`, API keys, personal member rosters, `.db` files, logs, or compiled `dist/` artifacts.

## Pull Request Process

1. Fork the repository and create a descriptive branch from `main` (e.g., `fix/csv-import-edge-case` or `feat/shift-export-filter`).
2. Write or update tests covering your changes in `tests/`.
3. Verify `ruff check .` and `pytest -v` pass locally.
4. Open a pull request using the PR template and describe the motivation, changes, and verification steps.
