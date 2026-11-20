# Security Policy

## Security Model & Intended Deployment

Club Tracker is designed as a **local-first, single-user desktop and localhost web application**:

- By default, both `desktop.py` and the launcher scripts (`run.bat`, `run.sh`) bind Uvicorn strictly to the loopback interface (`127.0.0.1`).
- **Do not expose this server directly to untrusted public networks** without adding an authenticated reverse proxy, as the local API endpoints (including settings, member roster export, and workspace reset) assume a trusted local operator.
- Your `GEMINI_API_KEY` is stored locally in `.env` and sent only to Google's Gemini API over HTTPS when scanning timetables or testing key connectivity.
- All member rosters and schedules are stored locally in SQLite (`club_scheduler.db`). Never commit `.env`, `club_scheduler.db`, or exported member CSV/JSON backups containing personal data (names, phone numbers, emails, Discord handles) to public repositories.
- When building standalone executables with `build_desktop.py`, `.env` is **not** copied into `dist/` by default. Do not pass `--include-env` when packaging builds for distribution.

## Reporting a Vulnerability

If you discover a security vulnerability in this repository:

1. **Do not open a public GitHub issue** disclosing sensitive exploit details.
2. Report the issue privately via GitHub's **Security Advisories** ("Report a vulnerability" under the repository's **Security** tab), or contact the repository owner directly.
3. Include a clear description of the vulnerability, reproduction steps, potential impact, and any suggested remediation.

Maintainers will acknowledge receipt as soon as possible and work with you to validate and resolve the issue before public disclosure.
