from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from openpyxl import Workbook, load_workbook

try:
    from supabase import Client as SupabaseClient
    from supabase import create_client as create_supabase_client
except Exception:
    SupabaseClient = None
    create_supabase_client = None


load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("aadhar_scanner.main")

AZURE_OCR_URL = "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=read"
AZURE_CAPTION_URL = "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=denseCaptions"
AZURE_HEADERS = {"api-call-origin": "Microsoft.Cognitive.CustomVision.Portal"}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
EXTERNAL_CHAT_ID = os.getenv("EXTERNAL_CHAT_ID", "").strip()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""
TELEGRAM_FILE_BASE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else ""

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    or os.getenv("SUPABASE_KEY", "").strip()
)
SUPABASE_PARSE_TABLE = os.getenv("SUPABASE_PARSE_TABLE", "aadhaar_parsed").strip()
SUPABASE_FORWARD_CONFIG_TABLE = os.getenv("SUPABASE_FORWARD_CONFIG_TABLE", "aadhaar_forward_configs").strip()
SUPABASE_FORWARD_LOG_TABLE = os.getenv("SUPABASE_FORWARD_LOG_TABLE", "aadhaar_forward_logs").strip()
LOCAL_EXCEL_ENABLED = os.getenv("LOCAL_EXCEL_ENABLED", "false").strip().lower() == "true"
LOCAL_EXCEL_FILE = os.getenv("LOCAL_EXCEL_FILE", "aadhaar_parsed.xlsx").strip()
LOCAL_EXCEL_SHEET = os.getenv("LOCAL_EXCEL_SHEET", "aadhaar_parsed").strip()

MAX_UPLOAD_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(5 * 1024 * 1024)))
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
CORS_ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
ADMIN_TELEGRAM_USER_IDS = {
    int(item.strip())
    for item in os.getenv("ADMIN_TELEGRAM_USER_IDS", "").split(",")
    if item.strip().isdigit()
}

session = requests.Session()


class ParseResult(BaseModel):
    name: str
    dob: str
    uid: str
    gender: str


class ParseListResponse(BaseModel):
    total: int
    is_admin: bool
    records: list[dict[str, Any]]


class ForwardingConfigInput(BaseModel):
    enabled: bool = False
    method: str = "POST"
    endpoint_url: str = ""
    headers_json: dict[str, Any] = Field(default_factory=dict)
    body_template_json: dict[str, Any] = Field(default_factory=dict)
    show_detailed_errors: bool = False


class ParseMutationInput(BaseModel):
    name: str
    dob: str
    uid: str
    gender: str
    source: str = "manual"


@dataclass
class TelegramUserCtx:
    user_id: int
    username: str | None
    first_name: str | None
    is_admin: bool


# Verhoeff tables
_v_multiplication = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_v_permutation = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def validate_verhoeff(number: str) -> bool:
    try:
        if not number.isdigit() or len(number) != 12:
            return False
        if number[0] in {"0", "1"}:
            return False
        c = 0
        for i, item in enumerate(reversed(number)):
            c = _v_multiplication[c][_v_permutation[i % 8][int(item)]]
        return c == 0
    except Exception:
        return False


def extract_valid_uid_from_text(text: str) -> str:
    digit_stream = re.sub(r"\D", "", text or "")
    if len(digit_stream) < 12:
        return ""
    for i in range(len(digit_stream) - 11):
        candidate = digit_stream[i : i + 12]
        if validate_verhoeff(candidate):
            return candidate
    return ""


def normalize_gender(value: str) -> str:
    token = (value or "").strip().upper()
    if token in {"MALE", "M"}:
        return "Male"
    if token in {"FEMALE", "F"}:
        return "Female"
    if token in {"OTHER", "O"}:
        return "Other"
    raise ValueError("gender must be MALE/FEMALE/OTHER")


def validate_dob(value: str) -> str:
    raw = (value or "").strip()
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
        raise ValueError("dob must be in DD/MM/YYYY format")
    try:
        datetime.strptime(raw, "%d/%m/%Y")
    except Exception as exc:
        raise ValueError("dob is invalid") from exc
    return raw


def validate_source(value: str) -> str:
    allowed = {"api", "webhook", "manual"}
    token = (value or "").strip().lower()
    if token not in allowed:
        raise ValueError("source must be one of: api, webhook, manual")
    return token


def validated_parse_payload(inp: ParseMutationInput) -> dict[str, str]:
    name = " ".join((inp.name or "").strip().split())
    if not name:
        raise ValueError("name is required")
    uid = re.sub(r"\D", "", inp.uid or "")
    if not validate_verhoeff(uid):
        raise ValueError("uid must be a valid 12-digit Aadhaar with Verhoeff check")
    return {
        "name": name,
        "dob": validate_dob(inp.dob),
        "uid": uid,
        "gender": normalize_gender(inp.gender),
        "source": validate_source(inp.source),
    }


def _token_context(record: dict[str, Any]) -> dict[str, str]:
    return {
        "uid": str(record.get("uid", "")),
        "name": str(record.get("name", "")),
        "dob": str(record.get("dob", "")),
        "gender": str(record.get("gender", "")),
        "source": str(record.get("source", "")),
        "created_at": str(record.get("created_at", "")),
        "telegram_user_id": str(record.get("telegram_user_id", "")),
        "telegram_username": str(record.get("telegram_username", "")),
        "record_id": str(record.get("id", "")),
    }


def _render_tokens(value: Any, ctx: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for key, v in ctx.items():
            out = out.replace(f"%{key}%", v)
        return out
    if isinstance(value, list):
        return [_render_tokens(item, ctx) for item in value]
    if isinstance(value, dict):
        return {k: _render_tokens(v, ctx) for k, v in value.items()}
    return value


def _extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def _looks_like_image(data: bytes) -> bool:
    return any(
        (
            data.startswith(b"\xff\xd8\xff"),
            data.startswith(b"\x89PNG\r\n\x1a\n"),
            data.startswith(b"RIFF") and b"WEBP" in data[:16],
        )
    )


def _escape_markdown(text: str) -> str:
    return re.sub(r"([_*`\[])", r"\\\1", text or "")


def telegram_api(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
    resp = session.post(f"{TELEGRAM_API_BASE}/{method}", json=payload or {}, timeout=60)
    if resp.status_code != 200:
        logger.error("Telegram API Error: %s - %s", resp.status_code, resp.text)
    resp.raise_for_status()
    return resp.json()


def send_message(chat_id: int | str, text: str, reply_to_message_id: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    return telegram_api("sendMessage", payload)


def edit_message_text(chat_id: int | str, message_id: int, text: str) -> None:
    telegram_api(
        "editMessageText",
        {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
        },
    )


def get_file_bytes(file_id: str) -> bytes:
    meta = telegram_api("getFile", {"file_id": file_id})
    file_path = meta["result"]["file_path"]
    file_url = f"{TELEGRAM_FILE_BASE}/{file_path}"
    response = session.get(file_url, timeout=60)
    response.raise_for_status()
    return response.content


def send_photo_with_caption(chat_id: int | str, image_bytes: bytes, caption: str) -> None:
    files = {"photo": ("source.jpg", image_bytes, "image/jpeg")}
    data = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "Markdown"}
    response = session.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files, timeout=60)
    response.raise_for_status()
    resp_data = response.json()
    if not resp_data.get("ok"):
        raise RuntimeError(f"Telegram sendPhoto error: {resp_data}")


class OCRParser:
    def __init__(self) -> None:
        self.session = requests.Session()

    def detect_card(self, image_bytes: bytes):
        response = self.session.post(
            AZURE_CAPTION_URL,
            headers=AZURE_HEADERS,
            files={"file": ("image.jpg", image_bytes, "image/jpeg")},
            timeout=60,
        )
        response.raise_for_status()
        values = response.json().get("denseCaptionsResult", {}).get("values", [])
        matches = []
        for item in values:
            text = item.get("text", "").lower()
            if any(token in text for token in ("id card", "identity card", "card", "document")):
                box = item.get("boundingBox", {})
                matches.append({"item": item, "area": box.get("w", 0) * box.get("h", 0)})
        if not matches:
            return None
        return min(matches, key=lambda x: x["area"])["item"]["boundingBox"]

    @staticmethod
    def crop_image_bytes(image_bytes: bytes, x: int, y: int, w: int, h: int) -> bytes:
        img = Image.open(BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        img_w, img_h = img.size
        x = max(0, int(x))
        y = max(0, int(y))
        right = min(x + int(w), img_w)
        bottom = min(y + int(h), img_h)
        cropped = img.crop((x, y, right, bottom))
        output = BytesIO()
        cropped.save(output, format="JPEG")
        return output.getvalue()

    def detect_text(self, image_bytes: bytes) -> tuple[dict[str, Any], bytes]:
        cropped = image_bytes
        try:
            box = self.detect_card(image_bytes)
            if box:
                cropped = self.crop_image_bytes(image_bytes, box["x"], box["y"], box["w"], box["h"])
        except Exception as exc:
            logger.warning("Card detection/cropping failed: %s", exc)

        response = self.session.post(
            AZURE_OCR_URL,
            headers=AZURE_HEADERS,
            files={"file": ("image.jpg", cropped, "image/jpeg")},
            timeout=60,
        )
        response.raise_for_status()
        return response.json(), cropped

    @staticmethod
    def extract_full_text(ocr_data: dict[str, Any]) -> str:
        lines: list[str] = []
        for block in ocr_data.get("readResult", {}).get("blocks", []):
            for line in block.get("lines", []):
                text = line.get("text", "")
                if text:
                    lines.append(text)
        return " ".join(lines).strip()

    @staticmethod
    def parse_with_regex(text: str) -> ParseResult:
        name = ""
        dob = ""
        uid = ""
        gender = ""

        name_match = re.search(r"[I|1|l]ndia[^a-zA-Z]*([a-zA-Z\s]+?)[^a-zA-Z]*D[O|0|o]B", text, flags=re.DOTALL)
        if name_match:
            name = name_match.group(1).strip()

        dob_match = re.search(r"D[O|0|o]B[:\s]+(\d{2}\/\d{2}\/\d{4})", text, flags=re.IGNORECASE)
        if dob_match:
            dob = dob_match.group(1).strip()

        uid_match = re.search(
            r"((?<=\s)\d{4}\s(?<=\s)\d{4}\s(?<=\s)\d{4})(?:\sVID\s*:\s*\d{4}\s\d{4}\s\d{4}\s\d{4})?",
            text,
            flags=re.IGNORECASE,
        )
        if uid_match:
            uid = re.sub(r"\D", "", uid_match.group(1))

        uid = extract_valid_uid_from_text(uid)

        upper_text = text.upper()
        if "FEMALE" in upper_text:
            gender = "Female"
        elif "MALE" in upper_text:
            gender = "Male"

        return ParseResult(name=name, dob=dob, uid=uid, gender=gender)

    def run_with_bytes(self, image_bytes: bytes) -> tuple[ParseResult, str, bytes]:
        ocr_result, processed_image = self.detect_text(image_bytes)
        full_text = self.extract_full_text(ocr_result)
        if not full_text:
            raise ValueError("No text detected by OCR")
        parsed = self.parse_with_regex(full_text)
        if not validate_verhoeff(parsed.uid):
            raise ValueError("No valid UID found using Verhoeff check")
        return parsed, full_text, processed_image


class SupabaseStore:
    def __init__(self) -> None:
        self.client = self._init_client()
        self.table = SUPABASE_PARSE_TABLE
        self.forward_config_table = SUPABASE_FORWARD_CONFIG_TABLE
        self.forward_log_table = SUPABASE_FORWARD_LOG_TABLE
        self.ready = self._validate_table()

    def _init_client(self):
        if not create_supabase_client:
            return None
        if not SUPABASE_URL or not SUPABASE_KEY:
            return None
        try:
            return create_supabase_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as exc:
            logger.exception("Supabase client init failed: %s", exc)
            return None

    def _validate_table(self) -> bool:
        if self.client is None:
            return False
        try:
            self.client.table(self.table).select("id").limit(1).execute()
            return True
        except Exception as exc:
            logger.error("Supabase table '%s' is not reachable: %s", self.table, exc)
            return False

    def save_parse(self, parsed: ParseResult, ocr_text: str, source: str, tg_user: TelegramUserCtx | None) -> dict[str, Any]:
        if self.client is None:
            return {"status": "failed: supabase_not_configured"}
        if not self.ready:
            return {"status": "failed: parse_table_not_ready"}
        if tg_user is None:
            return {"status": "failed: user_context_required"}

        payload = {
            "telegram_user_id": tg_user.user_id,
            "telegram_username": tg_user.username,
            "name": parsed.name.strip(),
            "dob": parsed.dob.strip(),
            "uid": parsed.uid.strip(),
            "gender": parsed.gender.strip(),
            "ocr_text": ocr_text,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        missing = [k for k in ("name", "dob", "uid", "gender") if not str(payload.get(k, "")).strip()]
        if missing:
            logger.error("Supabase save skipped: missing required fields: %s", ",".join(missing))
            return {"status": f"failed: required_field_missing:{','.join(missing)}"}

        try:
            result = self.client.table(self.table).insert(payload).execute()
            row = ((result.data or [{}])[0]) if hasattr(result, "data") else {}
            return {"status": "saved", "record": row or payload}
        except Exception as exc:
            logger.exception("Supabase save failed (user=%s uid=%s): %s", tg_user.user_id, payload["uid"], exc)
            return {"status": f"failed: {exc}"}

    def list_user_parses(self, user_id: int, limit: int, offset: int) -> list[dict[str, Any]]:
        result = (
            self.client.table(self.table)
            .select("id,telegram_user_id,telegram_username,name,dob,uid,gender,source,created_at,forward_status,forwarded_at,forward_error")
            .eq("telegram_user_id", user_id)
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
            .execute()
        )
        return result.data or []

    def admin_search(self, keyword: str, limit: int, offset: int) -> list[dict[str, Any]]:
        base_query = (
            self.client.table(self.table)
            .select("id,telegram_user_id,telegram_username,name,dob,uid,gender,source,created_at,forward_status,forwarded_at,forward_error")
            .order("created_at", desc=True)
            .range(offset, offset + limit - 1)
        )
        if keyword.strip():
            like_term = f"%{keyword.strip()}%"
            fields = ["telegram_username", "name", "dob", "uid", "gender", "source"]
            or_query = ",".join(f"{f}.ilike.{like_term}" for f in fields)
            result = base_query.or_(or_query).execute()
        else:
            result = base_query.execute()
        return result.data or []

    def get_forwarding_config(self, user_id: int) -> dict[str, Any]:
        defaults = ForwardingConfigInput().model_dump()
        try:
            result = (
                self.client.table(self.forward_config_table)
                .select("enabled,method,endpoint_url,headers_json,body_template_json,show_detailed_errors")
                .eq("telegram_user_id", user_id)
                .limit(1)
                .execute()
            )
            row = (result.data or [None])[0]
            return row or defaults
        except Exception:
            return defaults

    def save_forwarding_config(self, user_id: int, cfg: ForwardingConfigInput) -> dict[str, Any]:
        method = (cfg.method or "POST").upper().strip()
        if method not in {"POST", "PUT", "PATCH"}:
            raise ValueError("method must be POST/PUT/PATCH")
        if cfg.enabled and not (cfg.endpoint_url or "").strip():
            raise ValueError("endpoint_url is required when forwarding is enabled")
        payload = {
            "telegram_user_id": user_id,
            "enabled": bool(cfg.enabled),
            "method": method,
            "endpoint_url": (cfg.endpoint_url or "").strip(),
            "headers_json": cfg.headers_json or {},
            "body_template_json": cfg.body_template_json or {},
            "show_detailed_errors": bool(cfg.show_detailed_errors),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        result = (
            self.client.table(self.forward_config_table)
            .upsert(payload, on_conflict="telegram_user_id")
            .execute()
        )
        return ((result.data or [payload])[0]) if hasattr(result, "data") else payload

    def get_parse_for_user(self, record_id: int, user: TelegramUserCtx) -> dict[str, Any] | None:
        q = self.client.table(self.table).select("*").eq("id", record_id).limit(1)
        if not user.is_admin:
            q = q.eq("telegram_user_id", user.user_id)
        result = q.execute()
        return (result.data or [None])[0]

    def insert_manual_parse(self, user: TelegramUserCtx, payload: dict[str, str]) -> dict[str, Any]:
        row = {
            "telegram_user_id": user.user_id,
            "telegram_username": user.username,
            "name": payload["name"],
            "dob": payload["dob"],
            "uid": payload["uid"],
            "gender": payload["gender"],
            "ocr_text": "",
            "source": payload["source"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        result = self.client.table(self.table).insert(row).execute()
        return ((result.data or [row])[0]) if hasattr(result, "data") else row

    def update_parse(self, record_id: int, user: TelegramUserCtx, payload: dict[str, str]) -> dict[str, Any]:
        existing = self.get_parse_for_user(record_id, user)
        if not existing:
            raise PermissionError("record not found or not allowed")
        result = (
            self.client.table(self.table)
            .update(
                {
                    "name": payload["name"],
                    "dob": payload["dob"],
                    "uid": payload["uid"],
                    "gender": payload["gender"],
                    "source": payload["source"],
                }
            )
            .eq("id", record_id)
            .execute()
        )
        return ((result.data or [existing])[0]) if hasattr(result, "data") else existing

    def delete_parse(self, record_id: int, user: TelegramUserCtx) -> None:
        existing = self.get_parse_for_user(record_id, user)
        if not existing:
            raise PermissionError("record not found or not allowed")
        self.client.table(self.table).delete().eq("id", record_id).execute()

    def _update_forward_status(self, record_id: int, status: str, error: str = "", code: int | None = None) -> None:
        payload = {
            "forward_status": status,
            "forwarded_at": datetime.now(timezone.utc).isoformat(),
            "forward_error": (error or "")[:300],
        }
        try:
            self.client.table(self.table).update(payload).eq("id", record_id).execute()
        except Exception:
            pass
        try:
            self.client.table(self.forward_log_table).insert(
                {
                    "parse_id": record_id,
                    "status": status,
                    "response_code": code,
                    "error": (error or "")[:500],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception:
            pass

    def forward_record(self, record: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
        if not cfg.get("enabled"):
            return {"status": "skipped_disabled"}
        url = str(cfg.get("endpoint_url", "")).strip()
        if not url:
            return {"status": "skipped_missing_url"}
        method = str(cfg.get("method", "POST")).upper()
        headers = cfg.get("headers_json") or {}
        body_tpl = cfg.get("body_template_json") or {}
        if not isinstance(headers, dict) or not isinstance(body_tpl, dict):
            return {"status": "skipped_invalid_template"}
        ctx = _token_context(record)
        rendered_headers = _render_tokens(headers, ctx)
        rendered_body = _render_tokens(body_tpl, ctx)
        try:
            resp = session.request(method=method, url=url, headers=rendered_headers, json=rendered_body, timeout=20)
            ok = 200 <= resp.status_code < 300
            status = "success" if ok else "failed"
            err = "" if ok else f"http_{resp.status_code}:{resp.text[:200]}"
            if record.get("id"):
                self._update_forward_status(int(record["id"]), status, err, resp.status_code)
            return {"status": status, "code": resp.status_code, "error": err}
        except Exception as exc:
            if record.get("id"):
                self._update_forward_status(int(record["id"]), "failed", str(exc), None)
            return {"status": "failed", "error": str(exc)}


class LocalExcelStore:
    HEADERS = [
        "created_at",
        "telegram_user_id",
        "telegram_username",
        "name",
        "dob",
        "uid",
        "gender",
        "ocr_text",
        "source",
    ]

    def __init__(self, enabled: bool, path: str, sheet: str) -> None:
        self.enabled = enabled
        self.path = path
        self.sheet = sheet
        self.ready = self._init_file() if enabled else False

    def _init_file(self) -> bool:
        try:
            if not self.path:
                return False
            if not os.path.exists(self.path):
                wb = Workbook()
                ws = wb.active
                ws.title = self.sheet
                ws.append(self.HEADERS)
                wb.save(self.path)
                return True

            wb = load_workbook(self.path)
            ws = wb[self.sheet] if self.sheet in wb.sheetnames else wb.active
            first_row = [cell.value for cell in ws[1]]
            return first_row == self.HEADERS
        except Exception as exc:
            logger.exception("Local Excel init failed: %s", exc)
            return False

    def save_parse(self, parsed: ParseResult, ocr_text: str, source: str, tg_user: TelegramUserCtx | None) -> str:
        if not self.enabled:
            return "skipped: disabled"
        if not self.ready:
            return "failed: not_ready"
        if tg_user is None:
            return "failed: user_context_required"

        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "telegram_user_id": tg_user.user_id,
            "telegram_username": tg_user.username or "",
            "name": parsed.name.strip(),
            "dob": parsed.dob.strip(),
            "uid": parsed.uid.strip(),
            "gender": parsed.gender.strip(),
            "ocr_text": ocr_text,
            "source": source,
        }
        missing = [k for k in ("name", "dob", "uid", "gender") if not payload[k]]
        if missing:
            return f"failed: required_field_missing:{','.join(missing)}"

        try:
            wb = load_workbook(self.path)
            ws = wb[self.sheet] if self.sheet in wb.sheetnames else wb.active
            ws.append([payload.get(col, "") for col in self.HEADERS])
            wb.save(self.path)
            return "saved"
        except Exception as exc:
            logger.exception("Local Excel save failed: %s", exc)
            return f"failed: {exc}"


class StorageManager:
    def __init__(self) -> None:
        self.supabase = SupabaseStore()
        self.local_excel = LocalExcelStore(
            enabled=LOCAL_EXCEL_ENABLED,
            path=LOCAL_EXCEL_FILE,
            sheet=LOCAL_EXCEL_SHEET,
        )

    def save_parse(self, parsed: ParseResult, ocr_text: str, source: str, tg_user: TelegramUserCtx | None) -> dict[str, Any]:
        supabase_result = self.supabase.save_parse(parsed, ocr_text, source, tg_user)
        local_excel_result = self.local_excel.save_parse(parsed, ocr_text, source, tg_user)
        forwarding = {"status": "skipped"}
        if tg_user and supabase_result.get("status") == "saved":
            cfg = self.supabase.get_forwarding_config(tg_user.user_id)
            forwarding = self.supabase.forward_record(supabase_result.get("record") or {}, cfg)
        return {
            "supabase": supabase_result.get("status"),
            "local_excel": local_excel_result,
            "forwarding": forwarding,
            "record": supabase_result.get("record"),
        }

    def list_user_parses(self, user_id: int, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.supabase.list_user_parses(user_id, limit, offset)

    def admin_search(self, keyword: str, limit: int, offset: int) -> list[dict[str, Any]]:
        return self.supabase.admin_search(keyword, limit, offset)

    def get_forwarding_config(self, user_id: int) -> dict[str, Any]:
        return self.supabase.get_forwarding_config(user_id)

    def save_forwarding_config(self, user_id: int, cfg: ForwardingConfigInput) -> dict[str, Any]:
        return self.supabase.save_forwarding_config(user_id, cfg)

    def manual_insert(self, user: TelegramUserCtx, payload: dict[str, str]) -> dict[str, Any]:
        row = self.supabase.insert_manual_parse(user, payload)
        cfg = self.supabase.get_forwarding_config(user.user_id)
        self.supabase.forward_record(row, cfg)
        return row

    def update_parse(self, record_id: int, user: TelegramUserCtx, payload: dict[str, str]) -> dict[str, Any]:
        return self.supabase.update_parse(record_id, user, payload)

    def delete_parse(self, record_id: int, user: TelegramUserCtx) -> None:
        self.supabase.delete_parse(record_id, user)

    def test_forwarding(self, user_id: int, record: dict[str, Any]) -> dict[str, Any]:
        cfg = self.supabase.get_forwarding_config(user_id)
        return self.supabase.forward_record(record, cfg)

    @property
    def supabase_ready(self) -> bool:
        return bool(self.supabase.client and self.supabase.ready)


parser = OCRParser()
store = StorageManager()


def _startup_preflight() -> None:
    checks_ok: list[str] = []
    checks_warn: list[str] = []
    checks_fail: list[str] = []

    required_env = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_WEBHOOK_SECRET": TELEGRAM_WEBHOOK_SECRET,
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_KEY_OR_SERVICE_KEY": SUPABASE_KEY,
    }
    for key, val in required_env.items():
        if val:
            checks_ok.append(f"{key}: set")
        else:
            checks_fail.append(f"{key}: missing")

    if MAX_UPLOAD_SIZE_BYTES <= 0:
        checks_fail.append("MAX_UPLOAD_SIZE_BYTES must be > 0")
    else:
        checks_ok.append(f"MAX_UPLOAD_SIZE_BYTES={MAX_UPLOAD_SIZE_BYTES}")

    if not CORS_ALLOW_ORIGINS:
        checks_warn.append("CORS_ALLOW_ORIGINS is empty; default behavior may block browser clients")
    else:
        checks_ok.append(f"CORS_ALLOW_ORIGINS configured ({len(CORS_ALLOW_ORIGINS)} entries)")

    if not ADMIN_TELEGRAM_USER_IDS:
        checks_warn.append("ADMIN_TELEGRAM_USER_IDS is empty; admin search will be inaccessible")
    else:
        checks_ok.append(f"ADMIN_TELEGRAM_USER_IDS loaded ({len(ADMIN_TELEGRAM_USER_IDS)} users)")

    if LOCAL_EXCEL_ENABLED:
        if store.local_excel.ready:
            checks_ok.append(f"Local Excel enabled and ready ({LOCAL_EXCEL_FILE}:{LOCAL_EXCEL_SHEET})")
        else:
            checks_fail.append("Local Excel enabled but initialization failed")
    else:
        checks_ok.append("Local Excel disabled")

    if store.supabase_ready:
        checks_ok.append(f"Supabase table reachable ({SUPABASE_PARSE_TABLE})")
    else:
        checks_fail.append(f"Supabase table not reachable ({SUPABASE_PARSE_TABLE})")

    if TELEGRAM_BOT_TOKEN:
        try:
            bot_info = telegram_api("getMe")
            username = bot_info.get("result", {}).get("username", "unknown")
            checks_ok.append(f"Telegram bot API reachable (@{username})")
        except Exception as exc:
            checks_fail.append(f"Telegram getMe failed: {exc}")

    expected_routes = {
        "/health",
        "/demo",
        "/api/parse",
        "/api/me/parses",
        "/api/admin/search",
        "/telegram/webhook/{webhook_secret}",
    }
    registered_routes = {getattr(r, "path", "") for r in app.router.routes}
    missing_routes = sorted(path for path in expected_routes if path not in registered_routes)
    if missing_routes:
        checks_fail.append(f"Missing required routes: {', '.join(missing_routes)}")
    else:
        checks_ok.append("Required routes registered")

    logger.info("Startup preflight passed checks:\n- %s", "\n- ".join(checks_ok) if checks_ok else "none")
    if checks_warn:
        logger.warning("Startup preflight warnings:\n- %s", "\n- ".join(checks_warn))
    if checks_fail:
        logger.error("Startup preflight failed checks:\n- %s", "\n- ".join(checks_fail))
        raise RuntimeError("Startup preflight failed. Resolve configuration/connectivity issues and restart.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(_startup_preflight)
    yield


app = FastAPI(
    title="Aadhaar Scanner Unified Service",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


def verify_telegram_init_data(init_data: str) -> dict[str, Any]:
    if not init_data:
        raise HTTPException(status_code=401, detail={"error": "missing_init_data", "message": "Missing Telegram initData."})
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail={"error": "server_misconfigured", "message": "TELEGRAM_BOT_TOKEN is missing."})

    parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(parsed)
    hash_value = data.pop("hash", None)
    if not hash_value:
        raise HTTPException(status_code=401, detail={"error": "invalid_init_data", "message": "Missing hash in initData."})

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, hash_value):
        raise HTTPException(status_code=401, detail={"error": "invalid_init_data", "message": "Invalid Telegram signature."})

    user_raw = data.get("user")
    if not user_raw:
        raise HTTPException(status_code=401, detail={"error": "invalid_init_data", "message": "Missing Telegram user in initData."})

    user = json.loads(user_raw)
    user_id = int(user["id"])
    return {
        "user_id": user_id,
        "username": user.get("username"),
        "first_name": user.get("first_name"),
    }


def auth_user_ctx(authorization: str = Header(default="")) -> TelegramUserCtx:
    if not authorization.lower().startswith("tma "):
        raise HTTPException(status_code=401, detail={"error": "unauthorized", "message": "Expected Authorization: tma <initData>."})
    init_data = authorization[4:].strip()
    info = verify_telegram_init_data(init_data)
    return TelegramUserCtx(
        user_id=info["user_id"],
        username=info.get("username"),
        first_name=info.get("first_name"),
        is_admin=info["user_id"] in ADMIN_TELEGRAM_USER_IDS,
    )


def ensure_admin(user: TelegramUserCtx = Depends(auth_user_ctx)) -> TelegramUserCtx:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Admin access required."})
    return user


async def _parse_image_bytes(image_bytes: bytes) -> tuple[ParseResult, str, bytes]:
    try:
        return await run_in_threadpool(parser.run_with_bytes, image_bytes)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail={"error": "ocr_upstream_error", "message": "Azure OCR request failed."})
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"error": "processing_failed", "message": str(exc)}) from exc


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/parse")
async def parse_image(file: UploadFile = File(...), user: TelegramUserCtx = Depends(auth_user_ctx)) -> JSONResponse:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail={"error": "unsupported_media_type", "message": f"Allowed: {sorted(ALLOWED_CONTENT_TYPES)}"})
    ext = _extension(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail={"error": "unsupported_file_extension", "message": f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"})

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail={"error": "empty_file", "message": "Uploaded file is empty."})
    if len(image_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(status_code=413, detail={"error": "file_too_large", "message": f"Max upload is {MAX_UPLOAD_SIZE_BYTES} bytes."})
    if not _looks_like_image(image_bytes):
        raise HTTPException(status_code=415, detail={"error": "invalid_image_signature", "message": "Invalid JPEG/PNG/WEBP payload."})

    parsed, ocr_text, _processed_image = await _parse_image_bytes(image_bytes)
    storage_status = store.save_parse(parsed, ocr_text, source="api", tg_user=user)
    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "filename": file.filename,
            "content_type": file.content_type,
            "size_bytes": len(image_bytes),
            "ocr_text": ocr_text,
            "parsed": parsed.model_dump(),
            "storage_status": storage_status,
        },
    )


@app.get("/api/me/parses", response_model=ParseListResponse)
async def my_parses(limit: int = 25, offset: int = 0, user: TelegramUserCtx = Depends(auth_user_ctx)) -> ParseListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail={"error": "invalid_limit", "message": "limit must be between 1 and 200."})
    if offset < 0:
        raise HTTPException(status_code=400, detail={"error": "invalid_offset", "message": "offset must be >= 0."})
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})

    records = await run_in_threadpool(store.list_user_parses, user.user_id, limit, offset)
    return ParseListResponse(total=len(records), is_admin=user.is_admin, records=records)


@app.get("/api/admin/search", response_model=ParseListResponse)
async def admin_search(keyword: str = "", limit: int = 50, offset: int = 0, user: TelegramUserCtx = Depends(ensure_admin)) -> ParseListResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail={"error": "invalid_limit", "message": "limit must be between 1 and 200."})
    if offset < 0:
        raise HTTPException(status_code=400, detail={"error": "invalid_offset", "message": "offset must be >= 0."})
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})

    records = await run_in_threadpool(store.admin_search, keyword, limit, offset)
    return ParseListResponse(total=len(records), is_admin=True, records=records)


@app.get("/api/me/forwarding-config")
async def get_my_forwarding_config(user: TelegramUserCtx = Depends(auth_user_ctx)) -> dict[str, Any]:
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})
    cfg = await run_in_threadpool(store.get_forwarding_config, user.user_id)
    return {"status": "success", "config": cfg}


@app.put("/api/me/forwarding-config")
async def put_my_forwarding_config(payload: ForwardingConfigInput, user: TelegramUserCtx = Depends(auth_user_ctx)) -> dict[str, Any]:
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})
    try:
        saved = await run_in_threadpool(store.save_forwarding_config, user.user_id, payload)
        return {"status": "success", "config": saved}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_config", "message": str(exc)})


@app.post("/api/me/forwarding-test")
async def post_forwarding_test(payload: ParseMutationInput, user: TelegramUserCtx = Depends(auth_user_ctx)) -> dict[str, Any]:
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})
    try:
        parsed = validated_parse_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_payload", "message": str(exc)})
    record = {
        "id": "test",
        "telegram_user_id": user.user_id,
        "telegram_username": user.username or "",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **parsed,
    }
    result = await run_in_threadpool(store.test_forwarding, user.user_id, record)
    return {"status": "success", "forwarding": result}


@app.post("/api/me/parses")
async def create_manual_parse(payload: ParseMutationInput, user: TelegramUserCtx = Depends(auth_user_ctx)) -> dict[str, Any]:
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})
    try:
        validated = validated_parse_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_payload", "message": str(exc)})
    row = await run_in_threadpool(store.manual_insert, user, validated)
    return {"status": "success", "record": row}


@app.patch("/api/me/parses/{record_id}")
async def update_parse_record(record_id: int, payload: ParseMutationInput, user: TelegramUserCtx = Depends(auth_user_ctx)) -> dict[str, Any]:
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})
    try:
        validated = validated_parse_payload(payload)
        row = await run_in_threadpool(store.update_parse, record_id, user, validated)
        return {"status": "success", "record": row}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_payload", "message": str(exc)})
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": str(exc)})


@app.delete("/api/me/parses/{record_id}")
async def delete_parse_record(record_id: int, user: TelegramUserCtx = Depends(auth_user_ctx)) -> dict[str, Any]:
    if not store.supabase_ready:
        raise HTTPException(status_code=503, detail={"error": "storage_not_configured", "message": "Supabase is not configured or table is unavailable."})
    try:
        await run_in_threadpool(store.delete_parse, record_id, user)
        return {"status": "success"}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": str(exc)})


async def _process_telegram_message(msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    sender = msg.get("from") or {}
    user_id = sender.get("id")
    username = sender.get("username")
    first_name = sender.get("first_name") or "User"
    source_message_id = msg.get("message_id")

    photos = msg.get("photo") or []
    document = msg.get("document") or {}
    file_id = None
    if photos:
        file_id = photos[-1].get("file_id")
    elif document and str(document.get("mime_type", "")).startswith("image/"):
        file_id = document.get("file_id")

    if not file_id:
        send_message(
            chat_id,
            f"Hi {_escape_markdown(first_name)}, send me an Aadhaar image.",
            reply_to_message_id=source_message_id if isinstance(source_message_id, int) else None,
        )
        return

    try:
        image_bytes = get_file_bytes(file_id)
        parsed, ocr_text, processed_image = await _parse_image_bytes(image_bytes)
        user_ctx = TelegramUserCtx(
            user_id=int(user_id),
            username=username,
            first_name=first_name,
            is_admin=int(user_id) in ADMIN_TELEGRAM_USER_IDS,
        )
        storage_status = store.save_parse(parsed, ocr_text, source="webhook", tg_user=user_ctx)

        supabase_status = storage_status.get("supabase", "unknown")
        excel_status = storage_status.get("local_excel", "unknown")
        compact_storage = "Saved"
        if str(supabase_status).startswith("failed"):
            compact_storage = "Save issue"
        elif str(excel_status).startswith("failed"):
            compact_storage = "Saved (sheet issue)"
        elif "skipped" in str(excel_status):
            compact_storage = "Saved"

        parsed_text = (
            f"*Name:* *{_escape_markdown(parsed.name)}*\n"
            f"*Date of Birth:* {_escape_markdown(parsed.dob)}\n"
            f"*Gender:* {_escape_markdown(parsed.gender)}\n"
            f"*Aadhaar No:* `{_escape_markdown(parsed.uid)}`\n"
            f"*Storage:* {_escape_markdown(compact_storage)}"
        )

        final_text = f"✅ *Aadhaar Parsed Successfully*\n\n{parsed_text}"
        send_message(
            chat_id,
            final_text,
            reply_to_message_id=source_message_id if isinstance(source_message_id, int) else None,
        )

        if EXTERNAL_CHAT_ID:
            sender_label = f"@{username}" if username else (first_name or "Unknown")
            admin_caption = (
                "*Parsed Aadhaar Data*\n\n"
                f"{parsed_text}\n"
                f"*Sent by:* {_escape_markdown(sender_label)}\n"
                f"*Telegram User ID:* `{int(user_id)}`"
            )
            try:
                send_photo_with_caption(EXTERNAL_CHAT_ID, processed_image, admin_caption)
            except Exception as notify_exc:
                logger.exception("Admin notification forwarding failed: %s", notify_exc)
    except Exception as exc:
        logger.exception("Webhook processing failed: %s", exc)
        failure_text = (
            "❌ Could not parse a valid Aadhaar.\n\n"
            "Please send a clearer Aadhaar image with visible number and DOB."
        )
        send_message(
            chat_id,
            failure_text,
            reply_to_message_id=source_message_id if isinstance(source_message_id, int) else None,
        )


@app.post("/telegram/webhook/{webhook_secret}")
async def telegram_webhook(webhook_secret: str, payload: dict[str, Any]) -> dict[str, bool]:
    if not TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail={"error": "server_misconfigured", "message": "TELEGRAM_WEBHOOK_SECRET is missing."})
    if webhook_secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Invalid webhook secret."})

    message = payload.get("message") or payload.get("edited_message")
    if message:
        await _process_telegram_message(message)
    return {"ok": True}


DEMO_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>Aadhaar Scanner Mini App</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://code.iconify.design/iconify-icon/1.0.8/iconify-icon.min.js"></script>
  <style>
    :root {
      --bg: #101922;
      --surface: #172433;
      --surface-2: #1f3146;
      --ink: #e8f1fa;
      --muted: #9fb0c2;
      --line: #2c4058;
      --accent: #4fb2ff;
      --accent-soft: #23384f;
      --success: #2f7d53;
      --danger: #a44747;
      --radius: 14px;
      --nav-h: 64px;
    }
    [data-theme="warm"] {
      --bg: #f5f3ef;
      --surface: #fffdf9;
      --ink: #24211f;
      --muted: #786f67;
      --line: #ebe3d8;
      --accent: #7d6b5a;
      --accent-soft: #f2ece4;
    }
    [data-theme="soft-dark"] {
      --bg: #1f2124;
      --surface: #2a2d31;
      --ink: #f5f4f2;
      --muted: #b8bcc3;
      --line: #3a3f45;
      --accent: #8aa4c2;
      --accent-soft: #313a44;
      --success: #80c89f;
      --danger: #e29595;
    }

    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: "Manrope", "Segoe UI", ui-sans-serif, system-ui, sans-serif;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    .app {
      max-width: 920px;
      margin: 0 auto;
      min-height: 100%;
      display: grid;
      grid-template-rows: auto 1fr auto;
      padding-bottom: calc(var(--nav-h) + env(safe-area-inset-bottom) + 12px);
    }

    .topbar {
      position: sticky;
      top: 0;
      z-index: 30;
      backdrop-filter: blur(16px);
      background: color-mix(in srgb, var(--surface) 92%, transparent);
      border-bottom: 1px solid var(--line);
      padding: env(safe-area-inset-top) 12px 10px;
    }

    .topbar-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 44px;
    }

    .brand {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
      letter-spacing: -.01em;
      font-size: 1rem;
    }

    .icon-btn, .btn {
      border-radius: 11px;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--ink);
      padding: 9px 12px;
      font-size: .9rem;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      white-space: nowrap;
      transition: transform .16s ease, background-color .2s ease, border-color .2s ease;
    }
    .btn:active, .icon-btn:active { transform: scale(.98); }

    .btn.primary {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      font-weight: 600;
    }

    .btn:disabled, .icon-btn:disabled { opacity: .58; cursor: not-allowed; }

    .whoami {
      color: var(--muted);
      font-size: .84rem;
      margin-top: 6px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .hero {
      text-align: center;
      padding: 8px 0 14px;
    }
    .hero-avatar {
      width: 72px;
      height: 72px;
      border-radius: 50%;
      border: 2px solid var(--line);
      object-fit: cover;
      display: block;
      margin: 0 auto 8px;
      background: var(--surface-2);
    }
    .hero-name {
      margin: 0;
      font-size: 1.25rem;
      font-weight: 800;
    }
    .hero-meta {
      margin: 4px 0 0;
      color: var(--muted);
      font-size: .86rem;
      line-height: 1.35;
    }

    .popup {
      position: fixed;
      inset: 0;
      z-index: 50;
      display: none;
      align-items: flex-start;
      justify-content: flex-end;
      padding: calc(env(safe-area-inset-top) + 58px) 12px 12px;
      background: rgba(0,0,0,.18);
    }
    .popup.open { display: flex; }

    .popup-menu {
      width: min(460px, 95vw);
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 14px 30px rgba(0,0,0,.12);
      padding: 8px;
      display: grid;
      gap: 8px;
      animation: popupIn .18s ease;
    }
    @keyframes popupIn { from { opacity:0; transform: translateY(-6px);} to { opacity:1; transform: translateY(0);} }

    .menu-item {
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: var(--surface);
      color: var(--ink);
      padding: 10px 11px;
      font-size: .88rem;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
    }
    .submenu { display: none; border: 1px solid var(--line); border-radius: 10px; padding: 8px; }
    .submenu.open { display: block; animation: reveal .18s ease both; }
    .menu-title { font-size: .78rem; color: var(--muted); margin: 0 0 6px; }

    .content {
      padding: 12px;
      display: grid;
      align-content: start;
    }

    .tab-panel {
      display: none;
      opacity: 0;
      transform: translateY(6px);
      transition: opacity .22s ease, transform .22s ease;
    }
    .tab-panel.active {
      display: block;
      opacity: 1;
      transform: translateY(0);
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: 0 8px 22px rgba(0,0,0,.04);
      padding: 14px;
      margin-bottom: 12px;
    }

    .card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 10px;
    }

    .title {
      margin: 0;
      font-size: .98rem;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
    }

    .badge {
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--surface) 88%, var(--accent-soft));
      border-radius: 999px;
      padding: 4px 9px;
      font-size: .75rem;
      color: var(--muted);
      flex-shrink: 0;
    }

    .controls {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .input {
      width: 100%;
      min-width: 0;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--ink);
      border-radius: 11px;
      padding: 10px 12px;
      font-size: .9rem;
    }

    .status { font-size: .88rem; font-weight: 600; margin-top: 8px; color: var(--muted); }
    .ok { color: var(--success); }
    .bad { color: var(--danger); }

    .records-grid {
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }
    .record-tile {
      border: 1px solid var(--line);
      background: linear-gradient(165deg, color-mix(in srgb, var(--surface) 92%, white 8%), var(--surface-2));
      border-radius: 12px;
      padding: 11px;
      animation: reveal .26s ease both;
      transition: transform .16s ease, border-color .2s ease;
    }
    .record-tile:active {
      transform: scale(.992);
      border-color: color-mix(in srgb, var(--accent) 36%, var(--line));
    }
    .tile-top {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 8px;
      font-size: .76rem;
      color: var(--muted);
    }
    .tile-uid {
      font-size: 1.08rem;
      font-weight: 800;
      color: var(--accent);
      margin: 2px 0 2px;
      letter-spacing: .02em;
    }
    .tile-name {
      color: color-mix(in srgb, var(--ink) 82%, var(--muted));
      margin-bottom: 8px;
      font-size: .9rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tile-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .tile-item {
      border: 1px solid var(--line);
      border-radius: 9px;
      padding: 7px 8px;
      background: color-mix(in srgb, var(--surface) 94%, var(--accent-soft));
      min-width: 0;
    }
    .tile-item b {
      display: block;
      color: var(--muted);
      font-size: .68rem;
      margin-bottom: 3px;
      font-weight: 600;
      letter-spacing: .01em;
    }
    .tile-item span {
      display: block;
      font-size: .85rem;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tile-actions {
      margin-top: 8px;
      display: flex;
      justify-content: flex-end;
    }
    .icon-mini {
      border: 1px solid var(--line);
      background: color-mix(in srgb, var(--surface) 90%, var(--accent-soft));
      color: var(--ink);
      border-radius: 8px;
      width: 34px;
      height: 34px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
    }
    .toast {
      position: fixed;
      z-index: 80;
      right: 12px;
      bottom: calc(var(--nav-h) + env(safe-area-inset-bottom) + 14px);
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: .84rem;
      color: var(--ink);
      opacity: 0;
      transform: translateY(8px);
      transition: all .2s ease;
      pointer-events: none;
    }
    .toast.open { opacity: 1; transform: translateY(0); }
    .form-grid { display: grid; gap: 8px; }

    @keyframes reveal {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .pager {
      margin-top: 10px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
    }
    .pager .group { display: inline-flex; gap: 8px; align-items: center; }

    .bottom-nav {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 40;
      min-height: var(--nav-h);
      background: var(--surface);
      backdrop-filter: blur(10px);
      border-top: 1px solid var(--line);
      display: flex;
      justify-content: center;
      padding-bottom: env(safe-area-inset-bottom);
    }

    .bottom-wrap {
      width: min(920px, 100%);
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 8px;
      padding: 8px 12px;
    }

    .tab-btn {
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--muted);
      border-radius: 10px;
      font-size: .79rem;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 6px;
      transition: all .2s ease;
    }

    .tab-btn.active {
      color: var(--ink);
      border-color: var(--accent);
      background: color-mix(in srgb, var(--surface) 74%, var(--accent-soft));
      box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 35%, transparent);
    }

    .loader {
      width: 16px;
      height: 16px;
      border-radius: 999px;
      border: 2px solid color-mix(in srgb, var(--accent) 30%, transparent);
      border-top-color: var(--accent);
      animation: spin .8s linear infinite;
      display: inline-block;
      vertical-align: middle;
      margin-right: 6px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .skeleton {
      height: 36px;
      border-radius: 8px;
      background: color-mix(in srgb, var(--surface) 86%, var(--accent-soft));
      background-size: 200% 100%;
      animation: shimmer 1.3s infinite linear;
      margin-bottom: 8px;
    }
    @keyframes shimmer { to { background-position: -200% 0; } }

    @media (min-width: 860px) {
      .content { padding: 16px; }
      .topbar { border-radius: 0 0 12px 12px; }
      .records-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 620px) {
      .tile-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body data-theme="default">
  <div class="app">
    <header class="topbar">
      <div class="topbar-row">
        <div class="brand"><iconify-icon icon="solar:shield-user-outline"></iconify-icon> Aadhaar Scanner</div>
        <button id="menuBtn" class="icon-btn" aria-label="Settings"><iconify-icon icon="solar:settings-outline"></iconify-icon></button>
      </div>
      <div class="whoami" id="whoami">Authenticating...</div>
    </header>

    <main class="content">
      <section id="tab-history" class="tab-panel active">
        <div class="hero">
          <img id="userAvatar" class="hero-avatar" alt="User avatar" />
          <h2 id="heroName" class="hero-name">Aadhaar Scanner</h2>
          <p id="heroMeta" class="hero-meta">Secure OCR parsing assistant</p>
        </div>
        <div class="card">
          <div class="card-head">
            <h2 class="title"><iconify-icon icon="solar:history-outline"></iconify-icon> My Parsed Records</h2>
            <span class="badge" id="myCount">0 records</span>
          </div>
          <div class="controls">
            <input id="mySearch" class="input" placeholder="Search your parsed responses" />
          </div>
          <div id="myLoading" style="display:none">
            <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
          </div>
          <div id="myRows" class="records-grid"></div>
          <div class="pager">
            <span class="muted" id="myPageInfo">Page 1</span>
            <div class="group">
              <button id="myPrev" class="btn"><iconify-icon icon="solar:alt-arrow-left-outline"></iconify-icon> Prev</button>
              <button id="myNext" class="btn">Next <iconify-icon icon="solar:alt-arrow-right-outline"></iconify-icon></button>
            </div>
          </div>
        </div>
      </section>

      <section id="tab-upload" class="tab-panel">
        <div class="card">
          <div class="card-head">
            <h2 class="title"><iconify-icon icon="solar:document-add-outline"></iconify-icon> Demo Parse Upload</h2>
            <span class="badge">Secure</span>
          </div>
          <div class="controls">
            <input id="file" class="input" type="file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" />
            <button id="parseBtn" class="btn primary"><iconify-icon icon="solar:play-circle-outline"></iconify-icon> Parse Aadhaar</button>
          </div>
          <div id="parseStatus" class="status">Idle</div>
          <div class="card" style="margin:10px 0 0; padding:10px;">
            <div class="card-head" style="margin:0 0 6px;">
              <h3 class="title" style="font-size:.88rem; margin:0;"><iconify-icon icon="solar:code-square-outline"></iconify-icon> Parse Response</h3>
            </div>
            <pre id="parseResult" style="margin:0; max-height:260px; overflow:auto;">{}</pre>
          </div>
        </div>
      </section>

      <section id="tab-admin" class="tab-panel">
        <div class="card" id="adminCard" style="display:none">
          <div class="card-head">
            <h2 class="title"><iconify-icon icon="solar:magnifer-outline"></iconify-icon> Admin Search</h2>
            <span class="badge" id="adminCount">0 results</span>
          </div>
          <div class="controls">
            <input id="keyword" class="input" placeholder="Search username, name, UID, gender" />
            <button id="searchBtn" class="btn primary"><iconify-icon icon="solar:magnifer-outline"></iconify-icon> Search</button>
            <button id="clearSearchBtn" class="btn"><iconify-icon icon="solar:close-circle-outline"></iconify-icon> Clear</button>
          </div>
          <div id="adminRows" class="records-grid"></div>
          <div class="pager">
            <span class="muted" id="adminPageInfo">Page 1</span>
            <div class="group">
              <button id="adminPrev" class="btn"><iconify-icon icon="solar:alt-arrow-left-outline"></iconify-icon> Prev</button>
              <button id="adminNext" class="btn">Next <iconify-icon icon="solar:alt-arrow-right-outline"></iconify-icon></button>
            </div>
          </div>
        </div>
        <div class="card" id="adminLocked" style="display:none">
          <div class="title"><iconify-icon icon="solar:lock-keyhole-outline"></iconify-icon> Admin Access Required</div>
          <div class="whoami" style="margin-top:8px">This tab is available only to configured admin users.</div>
        </div>
      </section>

      <section id="tab-settings" class="tab-panel">
        <div class="card">
          <div class="card-head">
            <h2 class="title"><iconify-icon icon="solar:settings-outline"></iconify-icon> Theme & Customization</h2>
          </div>
          <div class="controls">
            <button class="btn" data-theme-btn="default">Default</button>
            <button class="btn" data-theme-btn="warm">Warm</button>
            <button class="btn" data-theme-btn="soft-dark">Soft Dark</button>
          </div>
        </div>
        <div class="card">
          <div class="card-head">
            <h2 class="title"><iconify-icon icon="solar:arrow-right-up-outline"></iconify-icon> Forwarding</h2>
          </div>
          <div class="form-grid">
            <label><input type="checkbox" id="fwEnabled" /> Enable forwarding on new parse</label>
            <select id="fwMethod" class="input"><option>POST</option><option>PUT</option><option>PATCH</option></select>
            <input id="fwUrl" class="input" placeholder="https://example.com/webhook" />
            <textarea id="fwHeaders" class="input" rows="3" placeholder='{"Authorization":"Bearer ..."}'></textarea>
            <textarea id="fwBody" class="input" rows="4" placeholder='{"uid":"%uid%","name":"%name%"}'></textarea>
            <label><input type="checkbox" id="fwDetailed" /> Show detailed errors</label>
            <div class="controls">
              <button class="btn primary" id="fwSaveBtn">Save</button>
              <button class="btn" id="fwTestBtn">Test</button>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-head">
            <h2 class="title"><iconify-icon icon="solar:database-outline"></iconify-icon> Data Management</h2>
          </div>
          <div class="form-grid">
            <input id="mRecordId" class="input" placeholder="Record ID (for edit/delete)" />
            <input id="mUid" class="input" placeholder="UID (12 digits)" />
            <input id="mName" class="input" placeholder="Name" />
            <input id="mDob" class="input" placeholder="DOB DD/MM/YYYY" />
            <select id="mGender" class="input"><option>Male</option><option>Female</option><option>Other</option></select>
            <select id="mSource" class="input"><option>manual</option><option>api</option><option>webhook</option></select>
            <div class="controls">
              <button class="btn primary" id="mInsertBtn">Insert</button>
              <button class="btn" id="mUpdateBtn">Update</button>
              <button class="btn" id="mDeleteBtn">Delete</button>
            </div>
          </div>
        </div>
      </section>
    </main>

    <nav class="bottom-nav">
      <div class="bottom-wrap">
        <button class="tab-btn active" data-tab="history"><iconify-icon icon="solar:history-outline"></iconify-icon> History</button>
        <button class="tab-btn" data-tab="upload"><iconify-icon icon="solar:upload-minimalistic-outline"></iconify-icon> Demo</button>
        <button class="tab-btn" data-tab="admin"><iconify-icon icon="solar:magnifer-outline"></iconify-icon> Admin</button>
        <button class="tab-btn" data-tab="settings"><iconify-icon icon="solar:settings-outline"></iconify-icon> Settings</button>
      </div>
    </nav>
  </div>

  <div id="popup" class="popup" aria-hidden="true">
    <div class="popup-menu" role="menu">
      <button class="menu-item" id="openSettingsPage"><iconify-icon icon="solar:settings-outline"></iconify-icon> Open Settings</button>
      <button class="menu-item" id="refreshAll"><iconify-icon icon="solar:refresh-outline"></iconify-icon> Refresh Data</button>
    </div>
  </div>
  <div id="toast" class="toast"></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) tg.ready();
const initData = tg?.initData || "";
const authHeader = { "Authorization": `tma ${initData}`, "Content-Type": "application/json" };
const parseAuthHeader = { "Authorization": `tma ${initData}` };

const parseBtn = document.getElementById('parseBtn');
const fileInput = document.getElementById('file');
const parseStatus = document.getElementById('parseStatus');
const parseResult = document.getElementById('parseResult');
const whoami = document.getElementById('whoami');
const toastEl = document.getElementById('toast');

const myLimit = 20;
let myOffset = 0;
const adminLimit = 20;
let adminOffset = 0;
let adminKeyword = '';
let isAdmin = false;
let myCachedRows = [];

async function readResponse(res) {
  const raw = await res.text();
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch { data = { raw }; }
  }
  if (!res.ok) {
    const msg = data?.detail?.message || data?.message || `Request failed (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data || {};
}

function toast(text) {
  toastEl.textContent = text;
  toastEl.classList.add('open');
  setTimeout(() => toastEl.classList.remove('open'), 1800);
}
function formatTimestamp(v) {
  if (!v) return '';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return `${String(d.getDate()).padStart(2, '0')}-${String(d.getMonth() + 1).padStart(2, '0')}-${d.getFullYear()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function setStatus(el, text, ok = true, loading = false) {
  el.classList.remove('ok', 'bad');
  el.innerHTML = loading ? `<span class="loader"></span>${text}` : text;
  el.classList.add(ok ? 'ok' : 'bad');
}
function setPageInfo(elId, offset, limit) { document.getElementById(elId).textContent = `Page ${Math.floor(offset / limit) + 1}`; }
function shareTextForRecord(r) {
  return `UID: ${r.uid || '-'}\nName: ${r.name || '-'}\nDOB: ${r.dob || '-'}\nGender: ${r.gender || '-'}\nSource: ${r.source || '-'}\nDate: ${formatTimestamp(r.created_at)}`;
}
async function shareRecord(id, source) {
  const row = source.find(x => String(x.id) === String(id));
  if (!row) return;
  const payload = shareTextForRecord(row);
  try {
    if (navigator.share) {
      await navigator.share({ title: 'Parsed Aadhaar Record', text: payload });
      toast('Shared');
      return;
    }
    await navigator.clipboard.writeText(payload);
    toast('Copied to clipboard');
  } catch (e) { toast('Share failed'); }
}
function tileHtml(r, isAdminTile = false) {
  const extra = isAdminTile ? `<div class="tile-name">${r.name || '-'} · @${r.telegram_username || '-'}</div>` : `<div class="tile-name">${r.name || '-'}</div>`;
  return `<article class="record-tile">
      <div class="tile-top"><span>${formatTimestamp(r.created_at)}</span><span>${r.source || '-'}</span></div>
      <div class="tile-uid">${r.uid || '-'}</div>
      ${extra}
      <div class="tile-grid">
        <div class="tile-item"><b>DOB</b><span>${r.dob || '-'}</span></div>
        <div class="tile-item"><b>Gender</b><span>${r.gender || '-'}</span></div>
      </div>
      <div class="tile-actions"><button class="icon-mini" data-share-id="${r.id}" data-admin="${isAdminTile ? '1':'0'}"><iconify-icon icon="solar:share-outline"></iconify-icon></button></div>
    </article>`;
}
function bindShareActions() {
  document.querySelectorAll('[data-share-id]').forEach(btn => {
    btn.onclick = async () => {
      const useAdmin = btn.getAttribute('data-admin') === '1';
      const source = useAdmin ? (window.__adminRows || []) : myCachedRows;
      await shareRecord(btn.getAttribute('data-share-id'), source);
    };
  });
}
function switchTab(tab) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById(`tab-${tab}`)?.classList.add('active');
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelector(`.tab-btn[data-tab="${tab}"]`)?.classList.add('active');
}
function renderMine() {
  const q = (document.getElementById('mySearch').value || '').trim().toLowerCase();
  const filtered = q ? myCachedRows.filter(r => [r.name, r.dob, r.uid, r.gender, r.source].some(v => String(v || '').toLowerCase().includes(q))) : myCachedRows;
  document.getElementById('myRows').innerHTML = filtered.map(r => tileHtml(r, false)).join('') || '<div class="tile-item"><b>Status</b><span>No records found</span></div>';
  document.getElementById('myCount').textContent = `${filtered.length} records`;
  bindShareActions();
}
async function loadMine() {
  document.getElementById('myLoading').style.display = 'block';
  const res = await fetch(`/api/me/parses?limit=${myLimit}&offset=${myOffset}`, { headers: parseAuthHeader });
  const data = await readResponse(res);
  document.getElementById('myLoading').style.display = 'none';
  myCachedRows = data.records || [];
  isAdmin = !!data.is_admin;
  renderMine();
  document.getElementById('myPrev').disabled = myOffset === 0;
  document.getElementById('myNext').disabled = myCachedRows.length < myLimit;
  setPageInfo('myPageInfo', myOffset, myLimit);
  const user = tg?.initDataUnsafe?.user || {};
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(' ').trim() || 'Telegram User';
  document.getElementById('userAvatar').src = user.photo_url || `https://ui-avatars.com/api/?background=1a2b3e&color=edf4fb&name=${encodeURIComponent(fullName)}`;
  document.getElementById('heroName').textContent = fullName;
  document.getElementById('heroMeta').textContent = `${user.username ? '@' + user.username : '@unknown'} • ID ${user.id || 'N/A'}`;
  whoami.textContent = `Authenticated. Admin: ${isAdmin ? 'Yes' : 'No'}`;
  document.getElementById('adminCard').style.display = isAdmin ? 'block' : 'none';
  document.getElementById('adminLocked').style.display = isAdmin ? 'none' : 'block';
  if (isAdmin) {
    try { await loadAdmin(); } catch (e) { toast(`Admin load failed: ${e.message}`); }
  }
}
async function loadAdmin() {
  if (!isAdmin) return;
  const res = await fetch(`/api/admin/search?keyword=${encodeURIComponent(adminKeyword)}&limit=${adminLimit}&offset=${adminOffset}`, { headers: parseAuthHeader });
  const data = await readResponse(res);
  const target = document.getElementById('adminRows');
  const rows = data.records || [];
  window.__adminRows = rows;
  target.innerHTML = rows.map(r => tileHtml(r, true)).join('') || '<div class="tile-item"><b>Status</b><span>No results found</span></div>';
  document.getElementById('adminCount').textContent = `${rows.length} results`;
  document.getElementById('adminPrev').disabled = adminOffset === 0;
  document.getElementById('adminNext').disabled = rows.length < adminLimit;
  setPageInfo('adminPageInfo', adminOffset, adminLimit);
  bindShareActions();
}
async function parseNow() {
  const f = fileInput.files?.[0];
  if (!f) return setStatus(parseStatus, 'Choose a file first', false);
  parseBtn.disabled = true; setStatus(parseStatus, 'Processing...', true, true);
  const fd = new FormData(); fd.append('file', f);
  try {
    const res = await fetch('/api/parse', { method: 'POST', headers: parseAuthHeader, body: fd });
    const data = await readResponse(res);
    parseResult.textContent = JSON.stringify(data, null, 2);
    setStatus(parseStatus, 'Parsed successfully', true);
    myOffset = 0; await loadMine(); switchTab('history');
  } catch (e) { setStatus(parseStatus, e.message, false); }
  finally { parseBtn.disabled = false; }
}
async function saveForwardingConfig() {
  const payload = {
    enabled: document.getElementById('fwEnabled').checked,
    method: document.getElementById('fwMethod').value,
    endpoint_url: document.getElementById('fwUrl').value.trim(),
    headers_json: JSON.parse(document.getElementById('fwHeaders').value || '{}'),
    body_template_json: JSON.parse(document.getElementById('fwBody').value || '{}'),
    show_detailed_errors: document.getElementById('fwDetailed').checked
  };
  const res = await fetch('/api/me/forwarding-config', { method: 'PUT', headers: authHeader, body: JSON.stringify(payload) });
  await readResponse(res);
  toast('Forwarding config saved');
}
async function loadForwardingConfig() {
  const res = await fetch('/api/me/forwarding-config', { headers: parseAuthHeader });
  const data = await readResponse(res);
  const c = data.config || {};
  document.getElementById('fwEnabled').checked = !!c.enabled;
  document.getElementById('fwMethod').value = c.method || 'POST';
  document.getElementById('fwUrl').value = c.endpoint_url || '';
  document.getElementById('fwHeaders').value = JSON.stringify(c.headers_json || {}, null, 2);
  document.getElementById('fwBody').value = JSON.stringify(c.body_template_json || {}, null, 2);
  document.getElementById('fwDetailed').checked = !!c.show_detailed_errors;
}
async function forwardingTest() {
  const payload = {
    uid: document.getElementById('mUid').value || '345613986248',
    name: document.getElementById('mName').value || 'Test User',
    dob: document.getElementById('mDob').value || '01/01/2000',
    gender: document.getElementById('mGender').value || 'Male',
    source: document.getElementById('mSource').value || 'manual'
  };
  const res = await fetch('/api/me/forwarding-test', { method: 'POST', headers: authHeader, body: JSON.stringify(payload) });
  const data = await readResponse(res);
  toast(`Forwarding test: ${data.forwarding?.status || 'ok'}`);
}
function mutationPayload() {
  return {
    uid: document.getElementById('mUid').value.trim(),
    name: document.getElementById('mName').value.trim(),
    dob: document.getElementById('mDob').value.trim(),
    gender: document.getElementById('mGender').value.trim(),
    source: document.getElementById('mSource').value.trim()
  };
}
async function insertRecord() {
  const res = await fetch('/api/me/parses', { method: 'POST', headers: authHeader, body: JSON.stringify(mutationPayload()) });
  await readResponse(res);
  toast('Record inserted'); await loadMine(); if (isAdmin) await loadAdmin();
}
async function updateRecord() {
  const id = Number(document.getElementById('mRecordId').value || 0); if (!id) throw new Error('Record ID required');
  const res = await fetch(`/api/me/parses/${id}`, { method: 'PATCH', headers: authHeader, body: JSON.stringify(mutationPayload()) });
  await readResponse(res);
  toast('Record updated'); await loadMine(); if (isAdmin) await loadAdmin();
}
async function deleteRecord() {
  const id = Number(document.getElementById('mRecordId').value || 0); if (!id) throw new Error('Record ID required');
  if (!confirm('Delete this record?')) return;
  const res = await fetch(`/api/me/parses/${id}`, { method: 'DELETE', headers: parseAuthHeader });
  await readResponse(res);
  toast('Record deleted'); await loadMine(); if (isAdmin) await loadAdmin();
}
function togglePopup(force) {
  const popup = document.getElementById('popup');
  const open = force !== undefined ? force : !popup.classList.contains('open');
  popup.classList.toggle('open', open);
  popup.setAttribute('aria-hidden', String(!open));
}
function setupInteractions() {
  document.getElementById('myPrev').onclick = async () => { myOffset = Math.max(0, myOffset - myLimit); await loadMine(); };
  document.getElementById('myNext').onclick = async () => { myOffset += myLimit; await loadMine(); };
  document.getElementById('adminPrev').onclick = async () => { adminOffset = Math.max(0, adminOffset - adminLimit); await loadAdmin(); };
  document.getElementById('adminNext').onclick = async () => { adminOffset += adminLimit; await loadAdmin(); };
  document.getElementById('searchBtn').onclick = async () => { adminKeyword = (document.getElementById('keyword').value || '').trim(); adminOffset = 0; await loadAdmin(); };
  document.getElementById('clearSearchBtn').onclick = async () => { document.getElementById('keyword').value = ''; adminKeyword = ''; adminOffset = 0; await loadAdmin(); };
  parseBtn.onclick = parseNow;
  document.getElementById('mySearch').addEventListener('input', renderMine);
  document.getElementById('menuBtn').onclick = () => togglePopup();
  document.getElementById('popup').addEventListener('click', (e) => { if (e.target.id === 'popup') togglePopup(false); });
  document.getElementById('openSettingsPage').onclick = () => { togglePopup(false); switchTab('settings'); };
  document.querySelectorAll('[data-theme-btn]').forEach(btn => btn.onclick = () => { document.body.setAttribute('data-theme', btn.getAttribute('data-theme-btn')); });
  document.getElementById('refreshAll').onclick = async () => { togglePopup(false); await loadMine(); };
  document.getElementById('fwSaveBtn').onclick = async () => { try { await saveForwardingConfig(); } catch (e) { toast(e.message); } };
  document.getElementById('fwTestBtn').onclick = async () => { try { await forwardingTest(); } catch (e) { toast(e.message); } };
  document.getElementById('mInsertBtn').onclick = async () => { try { await insertRecord(); } catch (e) { toast(e.message); } };
  document.getElementById('mUpdateBtn').onclick = async () => { try { await updateRecord(); } catch (e) { toast(e.message); } };
  document.getElementById('mDeleteBtn').onclick = async () => { try { await deleteRecord(); } catch (e) { toast(e.message); } };
  document.querySelectorAll('.tab-btn').forEach(btn => btn.onclick = () => switchTab(btn.getAttribute('data-tab')));
}
setupInteractions();
switchTab('history');
setStatus(parseStatus, 'Idle', true);
loadForwardingConfig().catch(() => {});
loadMine().catch(err => {
  const msg = err?.status === 401 || err?.status === 403 ? `Authentication failed: ${err.message}` : `Unable to load data: ${err.message}`;
  whoami.textContent = msg;
  whoami.classList.add('bad');
});
</script>
</body>
</html>"""


@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> HTMLResponse:
    return HTMLResponse(content=DEMO_HTML)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
