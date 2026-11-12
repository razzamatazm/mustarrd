# Repository Guidelines

## Project Structure & Module Organization
- `backend/` FastAPI service, with API routers in `backend/api/`, data models in `backend/models/`, and service logic in `backend/services/`.
- `frontend/` React + Vite app, source in `frontend/src/` and build output in `frontend/dist/`.
- `data/` local runtime data for non-Docker dev; Docker stores data under `./config/`, `./downloads/`, and `./completed/`.
- `docker-compose.yml` runs the full stack using GHCR images.
- `scripts/` and `tools/` contain helper assets.

## Build, Test, and Development Commands
- Backend dev:
  - `cd backend`
  - `python -m venv venv && source venv/bin/activate`
  - `pip install -r requirements.txt`
  - `python main.py` (serves API on `http://localhost:4177`)
  - Prefer the repo venv for backend commands and tests, e.g. `backend/venv/bin/python3.13 -m unittest ...`
- Frontend dev:
  - `cd frontend`
  - `npm install`
  - `npm run dev` (serves UI on `http://localhost:4178`)
- Docker:
  - `docker-compose up -d` (full stack, UI on `http://localhost:4178`)
  - Images are published by GitHub Actions using Buildx (see `.github/workflows/publish-images.yml`).
  - Local multi-arch builds can use `docker buildx build ... --push`.
  - Compose ports: backend `4177:4177`, frontend `4178:4178`.

## Coding Style & Naming Conventions
- Python: 4-space indentation; keep async code consistent with existing FastAPI patterns.
- React: 2-space indentation and standard React component naming (PascalCase for components, camelCase for hooks and helpers).
- No repo-wide formatter or linter is configured; keep changes aligned with surrounding file style.

## Testing Guidelines
- No automated test suite is present yet. If you add tests, place them under `backend/tests/` or `frontend/src/__tests__/` and document how to run them.
- When running backend tests or one-off backend Python commands, use the repo venv interpreter instead of the system Python.

## Commit & Pull Request Guidelines
- Commit messages follow short, imperative sentences (e.g., “Fix missing Path import”).
- PRs should include: a concise summary, steps to test, and screenshots for UI changes. Link related issues if applicable.

## Configuration & Local Data
- Backend supports `.env` in `backend/` for settings like `CATCHUP_DATABASE_URL` and download paths.
- Docker persists data in `./config/`, `./downloads/`, and `./completed/`; do not commit real credentials or generated media.
- Config uses the `CATCHUP_` env prefix (see `backend/config.py`) and defaults to SQLite at `/app/config/catchup_dvr.db`.
- Optional tool overrides: `CATCHUP_FFMPEG_PATH`, `CATCHUP_COMSKIP_PATH`.

## App Settings Notes
- App settings are stored in the `app_settings` table (created on startup).
- Lightweight schema updates are applied on startup (see `backend/database.py`).
- Recording offsets are configurable:
  - Defaults live in settings: `default_pre_padding_minutes` and `default_post_padding_minutes` (default 1/5).
  - The Download modal starts from these defaults but allows per-download overrides.
  - `scheduled_download_delay_minutes` is a global 0–120 minute wait after the scheduled end and post-padding; it defaults to 5, and 0 restores immediate dispatch.
- Enabling Comskip forces `transcode_enabled = true`; enabling commercial removal forces `remux_only = false`.
