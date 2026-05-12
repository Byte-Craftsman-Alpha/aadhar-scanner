# Release Notes

## v2.0.0 - 2026-05-12

### Major
- Added unified runtime `main.py` combining FastAPI API, Telegram webhook bot processing, and Telegram Mini App interface.
- Replaced polling-oriented architecture with webhook-first bot processing.

### Security
- Added strict Telegram WebApp `initData` signature verification for Mini App APIs.
- Added admin-only search enforcement via `ADMIN_TELEGRAM_USER_IDS`.
- Removed public open search API.

### API Changes
- Added `POST /telegram/webhook/{webhook_secret}`.
- Added `GET /api/me/parses` for user-scoped parsed records.
- Added `GET /api/admin/search` for admin-only search.
- Removed `GET /api/search` (public search).

### Data Model
- Migrated to per-user parse history schema (`telegram_user_id` ownership).
- Removed dependency on global UID uniqueness.
- Added optional per-user dedupe index (`telegram_user_id`, `uid`, `dob`).

### Operations
- Added SQL migration: `migrations/001_aadhaar_parsed_per_user.sql`.
- Updated docs and deployment guidance for webhook + Mini App model.
