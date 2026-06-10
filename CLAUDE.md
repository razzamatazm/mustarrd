# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Mustarrd is an IPTV catchup DVR web app. It connects to Xtream Codes IPTV servers, browses past EPG programs, and downloads catchup/timeshift streams with smart filename templating, commercial skip (Comskip), and GPU/CPU re-encoding (FFmpeg). It also downloads provider VOD (movies/series) and integrates with Plex (Plex-login users, push recordings to a Plex library).

## Development Commands

**Backend** (FastAPI, port 4177):
```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python main.py
```

**Frontend** (React + Vite, port 4178 in dev, proxies `/api` to 4177):
```bash
cd frontend
npm install
npm run dev
```

**Tests** (backend: stdlib `unittest`, no pytest; frontend: Vitest + React Testing Library):
```bash
cd backend
python -m unittest discover -s tests          # full suite, ~1000 tests, runs in seconds
python -m unittest tests.test_disk_full       # single module

cd frontend
npm test                                      # Vitest single run (test:watch for watch mode)
```
CI runs both suites on every PR (`.github/workflows/tests.yml`). Bug-fix PRs are expected to add a regression test in `backend/tests/`.

**Docker** (single container, backend serves the built frontend on 4177):
```bash
docker-compose up -d
```

## Architecture

### Download Pipeline
1. User picks a channel+EPG program → frontend calls `POST /api/downloads/`
2. `download_manager` (service) picks up the queued record and builds the Xtream catchup URL (`download_builder`)
3. The TS stream downloads; `post_processor` handles Comskip and/or FFmpeg transcode/remux
4. Finished file moves to the completed folder (named via `file_namer` / `vod_namer` templates)
5. Real-time progress flows over WebSocket at `/api/downloads/ws`

Disk-space preflight (`services/disk_space.py`) blocks downloads when space is low.

### Background Tasks (started in `backend/main.py` lifespan)
- `download_manager.process_queue()` — concurrent download queue; recovers in-progress tasks on restart
- `download_manager.process_post_queue()` — drives post-processing (the `post_processor` service does Comskip + FFmpeg work)
- `scheduled_manager.process_queue()` — fires scheduled recordings as their time arrives
- `epg_ingest_manager.process_queue()` — periodically refreshes EPG from Xtream for all accounts
- `server_log_bridge` — streams server logs to the frontend Logs page

### API Routers (`backend/api/`)
`auth`, `accounts`, `channels`, `downloads`, `schedules`, `vod`, `settings`, `epg`, `logs`, `onboarding`, `admin_users` (download-only user management), `admin_plex` (Plex server linking).

### Database & Migrations
SQLite via async SQLAlchemy. Schema is created and migrated on every startup in `backend/database.py` using lightweight `ALTER TABLE` checks — no migration framework. `app_settings` table holds global config (padding defaults, transcode flags, etc.). Account/Plex secrets are encrypted at rest with AES-GCM (`services/credential_crypto.py`).

Key constraint: enabling Comskip forces `transcode_enabled = true`; enabling commercial removal forces `remux_only = false`.

### Authentication & Security
Session-based auth in `backend/auth.py` and `backend/api/auth.py`. Two roles on the `users` table: `admin` and `download_only`. Download-only users can browse/schedule/download but not touch Settings; they sign in with username/password or via Plex OAuth PIN flow (`services/plex_service.py`, identities in `user_identities`).

- First run: `SetupLockdownMiddleware` returns 423 for all API routes until an admin password is set via `/api/auth/setup` (frontend Onboarding page)
- CSRF: unsafe methods require the token from `/api/auth/csrf` in a header, plus Origin validation (`backend/security.py`, `CSRFMiddleware` in `main.py`)
- Login/setup/Plex endpoints are rate-limited (`_enforce_rate_limit` in `api/auth.py`)
- Session cookie auto-sets `Secure` for HTTPS/forwarded-HTTPS; session secret auto-generated and persisted to the config dir
- Frontend: `ProtectedRoute` in `App.jsx`, checks `/api/auth/status` on load; `get_auth_context` re-verifies the role against the DB on every request

### Configuration
All settings use the `CATCHUP_` env prefix (see `backend/config.py`). A `.env` file in `backend/` is supported. Key vars:
- `CATCHUP_DATABASE_URL` — defaults to SQLite in `/app/config/` (Docker) or `data/` (local); `CATCHUP_DATA_ROOT` moves the local data dir
- `CATCHUP_DEFAULT_DOWNLOAD_FOLDER` / `CATCHUP_DEFAULT_COMPLETED_FOLDER`
- `CATCHUP_MAX_CONCURRENT_DOWNLOADS` (default: 2)
- `CATCHUP_FFMPEG_PATH`, `CATCHUP_COMSKIP_PATH` — override auto-detected tool paths (read in `post_processor.py`, not `config.py`)
- `CATCHUP_SESSION_SECRET`, `CATCHUP_CORS_ORIGINS`, `CATCHUP_DEBUG`
- `CATCHUP_DESKTOP_MODE=1` — desktop packaging mode (`desktop_server.py`, binds 127.0.0.1, downloads to `~/Downloads`)

Environment detection: Docker paths activate via `CATCHUP_DOCKER=1` or container runtime markers.

## Code Style
- Python: 4-space indentation; async throughout (FastAPI + SQLAlchemy AsyncIO)
- React: 2-space indentation; PascalCase components, camelCase hooks/helpers
- Mantine UI 7.x for all frontend components; TanStack React Query for data fetching
- No repo-wide formatter — match the style of the file you're editing

## Commits & PRs
- Short imperative commit messages (e.g., `Fix missing Path import`)
- PRs need: concise summary, steps to test, screenshots for UI changes (convention: `.github/pr-screenshots/`)
- User-facing changes get a `CHANGELOG.md` entry written in plain English ("What you would notice" / "What changed"), newest at top
