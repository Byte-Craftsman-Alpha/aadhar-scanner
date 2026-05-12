# Aadhaar Scanner Unified Server

Unified FastAPI service for Aadhaar OCR/parsing + Telegram bot webhook + Telegram Mini App.

## What This Service Does
- Accepts Aadhaar images from Telegram webhook and Mini App upload.
- Detects card region, runs OCR, extracts `name`, `dob`, `uid`, `gender`.
- Validates UID with Verhoeff checksum.
- Stores parsed records in Supabase with per-user ownership.
- Exposes Mini App APIs with strict Telegram `initData` authentication.
- Restricts search to admin users only.

## Runtime Entry
- `main.py` is the single runtime entrypoint.
- No polling service is required.

## Routes (6 total)
- `GET /health`
- `GET /demo`
- `POST /api/parse`
- `GET /api/me/parses`
- `GET /api/admin/search`
- `POST /telegram/webhook/{webhook_secret}`

## Security Model
- Mini App APIs require `Authorization: tma <initData>`.
- Server verifies Telegram initData HMAC with `TELEGRAM_BOT_TOKEN`.
- `/api/me/parses` returns only authenticated user records.
- `/api/admin/search` requires user id in `ADMIN_TELEGRAM_USER_IDS`.
- Webhook endpoint is protected by path secret `TELEGRAM_WEBHOOK_SECRET`.

## Supabase Schema
Run migration:

```sql
-- file: migrations/001_aadhaar_parsed_per_user.sql
create table if not exists public.aadhaar_parsed (
    id bigserial primary key,
    telegram_user_id bigint not null,
    telegram_username text,
    name text not null,
    dob text not null,
    uid text not null,
    gender text not null,
    ocr_text text,
    source text not null default 'api',
    created_at timestamptz not null default now()
);

create unique index if not exists ux_aadhaar_parsed_user_uid_dob
on public.aadhaar_parsed (telegram_user_id, uid, dob);
```

## Required Environment Variables
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY` (preferred) or `SUPABASE_KEY`
- `SUPABASE_PARSE_TABLE` (default `aadhaar_parsed`)
- `ADMIN_TELEGRAM_USER_IDS` (comma-separated numeric IDs)

Optional:
- `PUBLIC_BASE_URL`
- `MAX_UPLOAD_SIZE_BYTES`
- `CORS_ALLOW_ORIGINS`
- `LOCAL_EXCEL_ENABLED` (true/false)
- `LOCAL_EXCEL_FILE`
- `LOCAL_EXCEL_SHEET`

## Telegram Webhook Setup
Set webhook to your public URL:

```bash
curl -X POST "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
  -d "url=<PUBLIC_BASE_URL>/telegram/webhook/<TELEGRAM_WEBHOOK_SECRET>"
```

## Run
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Notes
- `app.py` and `telegram_listener.py` are legacy split services; `main.py` is the unified service.
- Keep service-role key server-side only.
