from __future__ import annotations

import base64
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
import json
from io import BytesIO

from PIL import Image, ImageOps
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel

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
logger = logging.getLogger("aadhar_scanner.app")

AZURE_OCR_URL = "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=read"
AZURE_CAPTION_URL = "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=denseCaptions"
HEADERS = {"api-call-origin": "Microsoft.Cognitive.CustomVision.Portal"}

MAX_UPLOAD_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(5 * 1024 * 1024)))
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]

# Verhoeff Algorithm Tables
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
_v_inverse = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def validate_verhoeff(number: str):
    """Validate a string of digits using the Verhoeff algorithm."""
    try:
        # Check if it's strictly numeric and 12 digits
        if not number.isdigit() or len(number) != 12:
            return False

        # Aadhaar specific: First digit cannot be 0 or 1
        if number[0] in ["0", "1"]:
            return False

        c = 0
        # Process the digits in reverse
        for i, item in enumerate(reversed(number)):
            c = _v_multiplication[c][_v_permutation[i % 8][int(item)]]

        # If the result is 0, the number is valid
        return c == 0
    except Exception:
        return False


def find_valid_substrings(large_string: str):
    """
    Scans a large string to find all 12-digit substrings
    that satisfy the Verhoeff check and specific constraints.
    """
    valid_matches = []

    # We need at least 12 characters to find a match
    if len(large_string) < 12:
        return valid_matches

    # Slide a window of 12 digits across the string
    for i in range(len(large_string) - 11):
        candidate = large_string[i : i + 12]

        # Check if the 12-character slice is valid
        if validate_verhoeff(candidate):
            valid_matches.append({"index": i, "value": candidate})

    return valid_matches


def extract_valid_uid_from_text(text: str) -> str:
    digit_stream = re.sub(r"\D", "", text or "")
    matches = find_valid_substrings(digit_stream)
    return matches[0]["value"] if matches else ""


class ParseResult(BaseModel):
    name: str
    dob: str
    uid: str
    gender: str


class StoredRecord(BaseModel):
    created_at: str
    filename: str
    content_type: str
    size_bytes: int
    ocr_text: str
    name: str
    dob: str
    uid: str
    gender: str


class SearchResponse(BaseModel):
    keyword: str
    total_matches: int
    backends_used: list[str]
    records: list[dict[str, Any]]


class StorageManager:
    EXCEL_HEADERS = [
        "created_at",
        "filename",
        "content_type",
        "size_bytes",
        "ocr_text",
        "name",
        "dob",
        "uid",
        "gender"
    ]

    def __init__(self) -> None:
        self.local_excel_enabled = (
            os.getenv("LOCAL_EXCEL_ENABLED", "false").strip().lower() == "true"
        )
        self.local_excel_file = os.getenv("LOCAL_EXCEL_FILE", "parsed_cards.xlsx").strip()
        self.local_excel_sheet = os.getenv("LOCAL_EXCEL_SHEET", "parsed_cards").strip()
        self.unique_fields = [
            field.strip()
            for field in os.getenv("LOCAL_EXCEL_UNIQUE_FIELDS", "uid").split(",")
            if field.strip()
        ]
        self.required_fields = [
            field.strip()
            for field in os.getenv("LOCAL_EXCEL_REQUIRED_FIELDS", "name,dob,uid,gender").split(",")
            if field.strip()
        ]
        self.local_excel_ready = self._init_local_excel() if self.local_excel_enabled else False

        self.supabase_table = os.getenv("SUPABASE_TABLE", "parsed_cards").strip()
        self.supabase_full_record_enabled = (
            os.getenv("SUPABASE_FULL_RECORD_ENABLED", "false").strip().lower() == "true"
        )
        self.supabase_client = self._init_supabase_client()
        self.supabase_parse_table = os.getenv("SUPABASE_PARSE_TABLE", "aadhaar_parsed").strip()
        self.supabase_parse_table_ready = self._validate_supabase_parse_table()

    def _init_local_excel(self) -> bool:
        if not self.local_excel_file:
            return False
        try:
            if not os.path.exists(self.local_excel_file):
                wb = Workbook()
                ws = wb.active
                ws.title = self.local_excel_sheet
                ws.append(self.EXCEL_HEADERS)
                wb.save(self.local_excel_file)
                return True

            wb = load_workbook(self.local_excel_file)
            ws = wb[self.local_excel_sheet] if self.local_excel_sheet in wb.sheetnames else wb.active
            first_row = [cell.value for cell in ws[1]]
            if first_row != self.EXCEL_HEADERS:
                return False
            return True
        except Exception:
            return False

    def _read_excel_rows(self) -> list[dict[str, Any]]:
        wb = load_workbook(self.local_excel_file)
        ws = wb[self.local_excel_sheet] if self.local_excel_sheet in wb.sheetnames else wb.active
        rows: list[dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row is None:
                continue
            item = {
                header: ("" if value is None else str(value))
                for header, value in zip(self.EXCEL_HEADERS, row)
            }
            if any(str(v).strip() for v in item.values()):
                rows.append(item)
        return rows

    def _validate_excel_constraints(self, payload: dict[str, Any]) -> str | None:
        for field in self.required_fields:
            if not str(payload.get(field, "")).strip():
                return f"required_field_missing:{field}"

        existing_rows = self._read_excel_rows()
        for field in self.unique_fields:
            target = str(payload.get(field, "")).strip().lower()
            if not target:
                continue
            for row in existing_rows:
                current = str(row.get(field, "")).strip().lower()
                if current and current == target:
                    return f"unique_constraint_violation:{field}"
        return None

    def _init_supabase_client(self):
        if not create_supabase_client:
            return None
        url = os.getenv("SUPABASE_URL", "").strip()
        key = (
            os.getenv("SUPABASE_SERVICE_KEY", "").strip()
            or os.getenv("SUPABASE_KEY", "").strip()
        )
        if not url or not key:
            return None
        try:
            return create_supabase_client(url, key)
        except Exception as exc:
            logger.exception("Supabase client initialization failed: %s", exc)
            return None

    def _validate_supabase_parse_table(self) -> bool:
        if self.supabase_client is None:
            return False

        try:
            self.supabase_client.table(self.supabase_parse_table).select("uid").limit(1).execute()
            logger.info("Supabase parse table is reachable: %s", self.supabase_parse_table)
            return True
        except Exception as exc:
            logger.error(
                "Supabase parse table '%s' is not reachable. "
                "Create it manually and verify SUPABASE_PARSE_TABLE. Error: %s",
                self.supabase_parse_table,
                exc,
            )
            return False

    def _save_parse_result_to_supabase(self, parsed: ParseResult) -> str:
        if self.supabase_client is None:
            return "failed: supabase_not_configured"

        if not self.supabase_parse_table_ready:
            return "failed: parse_table_not_ready"

        payload = {
            "name": parsed.name.strip(),
            "dob": parsed.dob.strip(),
            "uid": parsed.uid.strip(),
            "gender": parsed.gender.strip(),
        }

        missing = [k for k, v in payload.items() if not v]
        if missing:
            msg = f"failed: required_field_missing:{','.join(missing)}"
            logger.warning("Supabase save skipped due to missing required parse fields: %s", missing)
            return msg

        try:
            self.supabase_client.table(self.supabase_parse_table).upsert(
                payload,
                on_conflict="uid",
            ).execute()
            return "saved"
        except Exception as exc:
            logger.exception(
                "Supabase save failed for parsed result (uid=%s): %s",
                payload["uid"],
                exc,
            )
            return f"failed: {exc}"

    def available_backends(self) -> list[str]:
        backends: list[str] = []
        if self.local_excel_ready:
            backends.append("local_excel")
        if self.supabase_client is not None:
            backends.append("supabase")
        return backends

    @staticmethod
    def _as_record(
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        ocr_text: str,
        parsed: ParseResult,
    ) -> StoredRecord:
        return StoredRecord(
            created_at=datetime.now(timezone.utc).isoformat(),
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            ocr_text=ocr_text,
            **parsed.model_dump(),
        )

    def save(
        self,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
        ocr_text: str,
        parsed: ParseResult,
    ) -> dict[str, str]:
        record = self._as_record(
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            ocr_text=ocr_text,
            parsed=parsed,
        )

        status: dict[str, str] = {}
        payload = record.model_dump()

        if self.local_excel_ready:
            try:
                constraint_error = self._validate_excel_constraints(payload)
                if constraint_error:
                    status["local_excel"] = f"failed: {constraint_error}"
                else:
                    wb = load_workbook(self.local_excel_file)
                    ws = (
                        wb[self.local_excel_sheet]
                        if self.local_excel_sheet in wb.sheetnames
                        else wb.active
                    )
                    ws.append([payload.get(key, "") for key in self.EXCEL_HEADERS])
                    wb.save(self.local_excel_file)
                    status["local_excel"] = "saved"
            except Exception as exc:
                status["local_excel"] = f"failed: {exc}"

        if self.supabase_client is not None and self.supabase_full_record_enabled:
            try:
                self.supabase_client.table(self.supabase_table).insert(payload).execute()
                status["supabase"] = "saved"
            except Exception as exc:
                logger.exception("Supabase full-record save failed: %s", exc)
                status["supabase"] = f"failed: {exc}"
        elif self.supabase_client is not None:
            status["supabase"] = "skipped: full_record_disabled"

        status["supabase_parse_result"] = self._save_parse_result_to_supabase(parsed)

        return status

    def search(self, keyword: str, limit: int = 50) -> SearchResponse:
        term = keyword.strip().lower()
        if not term:
            raise ValueError("Keyword cannot be empty.")

        all_records: list[dict[str, Any]] = []
        backends_used: list[str] = []

        if self.local_excel_ready:
            backends_used.append("local_excel")
            try:
                rows = self._read_excel_rows()
                for row in rows:
                    haystack = " ".join(str(v) for v in row.values()).lower()
                    if term in haystack:
                        item = dict(row)
                        item["_source"] = "local_excel"
                        all_records.append(item)
            except Exception:
                pass

        if self.supabase_client is not None and self.supabase_full_record_enabled:
            backends_used.append("supabase")
            like_term = f"%{keyword.strip()}%"
            fields = [
                "name",
                "dob",
                "uid",
                "gender",
                "ocr_text",
                "filename",
                "content_type",
            ]
            or_query = ",".join(f"{field}.ilike.{like_term}" for field in fields)
            try:
                result = (
                    self.supabase_client.table(self.supabase_table)
                    .select("*")
                    .or_(or_query)
                    .limit(limit)
                    .execute()
                )
                rows = result.data or []
                for row in rows:
                    item = dict(row)
                    item["_source"] = "supabase"
                    all_records.append(item)
            except Exception:
                pass

        return SearchResponse(
            keyword=keyword,
            total_matches=len(all_records[:limit]),
            backends_used=backends_used,
            records=all_records[:limit],
        )


class OCRGroqParser:
    def __init__(self) -> None:
        self.session = requests.Session()

    def _decode_base64_source(self, source: str) -> bytes:
        value = source.strip()

        if value.startswith("data:") and "," in value:
            value = value.split(",", 1)[1]

        value = value.replace("\n", "").replace(" ", "")

        try:
            return base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 image data.") from exc

    def get_image_bytes(self, source: str) -> bytes:
        source = source.strip()

        if source.startswith(("http://", "https://")):
            response = self.session.get(source, timeout=30)
            response.raise_for_status()
            return response.content

        return self._decode_base64_source(source)

    def detect_card(self, image_bytes: bytes):
        files = {
            "file": ("image.jpg", image_bytes, "image/jpeg")
        }

        response = self.session.post(
            AZURE_CAPTION_URL,
            headers=HEADERS,
            files=files,
            timeout=60
        )

        response.raise_for_status()

        data = response.json()

        # print(json.dumps(data, indent=2))

        values = data.get("denseCaptionsResult", {}).get("values", [])

        matches = []

        for item in values:
            text = item.get("text", "").lower()

            # print("FOUND:", text)

            if (
                "id card" in text or
                "identity card" in text or
                "card" in text or
                "document" in text
            ):
                box = item.get("boundingBox", {})

                w = box.get("w", 0)
                h = box.get("h", 0)

                area = w * h

                matches.append({
                    "item": item,
                    "area": area
                })

        if not matches:
            return None

        # Select smallest area
        best_match = min(matches, key=lambda x: x["area"])

        # print("SELECTED:", best_match["item"])

        return best_match["item"]["boundingBox"]

    def crop_image_bytes(self, image_bytes, x, y, w, h):
        img = Image.open(BytesIO(image_bytes))

        # IMPORTANT: normalize EXIF rotation
        img = ImageOps.exif_transpose(img)

        # print("IMAGE SIZE:", img.size)
        # print("CROP:", x, y, w, h)

        img_width, img_height = img.size

        x = max(0, int(x))
        y = max(0, int(y))

        right = min(x + int(w), img_width)
        bottom = min(y + int(h), img_height)

        cropped = img.crop((x, y, right, bottom))

        output = BytesIO()

        cropped.save(output, format="JPEG")

        return output.getvalue()

    def detect_text(self, image_bytes: bytes) -> dict[str, Any]:
        box = self.detect_card(image_bytes)

        if (box):
            card_image = self.crop_image_bytes(
                image_bytes,
                x=box['x'],
                y=box['y'],
                w=box['w'],
                h=box['h']
            )
            image_bytes = card_image

        files = {"file": ("image.jpg", image_bytes, "image/jpeg")}
        response = self.session.post(AZURE_OCR_URL, headers=HEADERS, files=files, timeout=60)
        response.raise_for_status()
        return response.json()

    def extract_full_text(self, ocr_data: dict[str, Any]) -> str:
        lines: list[str] = []
        for block in ocr_data.get("readResult", {}).get("blocks", []):
            for line in block.get("lines", []):
                text = line.get("text", "")
                if text:
                    lines.append(text)
        return " ".join(lines).strip()

    def parse_with_regex(self, text: str) -> ParseResult:
        name = ""
        dob = ""
        uid = ""
        gender = ""

        name_match = re.search(r"[I|1|l]ndia[^a-zA-Z]*([a-zA-Z\s]+?)[^a-zA-Z]*D[O|0|o]B", text, flags=re.DOTALL)
        if name_match:
            name = name_match.group(1).strip()

        dob_match = re.search(r"DOB[:\s]+(\d{2}\/\d{2}\/\d{4})", text, flags=re.IGNORECASE)
        if dob_match:
            dob = dob_match.group(1).strip()

        # Prefer 12-digit Aadhaar grouping; keep the provided VID tail pattern shape.
        uid_match = re.search(
            r"((?<=\s)\d{4}\s(?<=\s)\d{4}\s(?<=\s)\d{4})(?:\sVID\s*:\s*\d{4}\s\d{4}\s\d{4}\s\d{4})?",
            text,
            flags=re.IGNORECASE,
        )
        if uid_match:
            print(text)
            uid = re.sub(r"\D", "", uid_match.group(1))
            self.UID = uid
            print(f"Detected UID: {uid}")

        upper_text = text.upper()
        if "FEMALE" in upper_text:
            gender = "Female"
        elif "MALE" in upper_text:
            gender = "Male"

        return ParseResult(
            name=name,
            dob=dob,
            uid=uid,
            gender=gender,
        )

    def run_with_bytes(self, image_bytes: bytes) -> tuple[ParseResult, str]:
        ocr_result = self.detect_text(image_bytes)
        full_text = self.extract_full_text(ocr_result)
        if not full_text:
            raise ValueError("No text detected by OCR.")
        parsed = self.parse_with_regex(full_text)
        valid_uid = extract_valid_uid_from_text(parsed.uid)
        if valid_uid:
            parsed.uid = valid_uid
        if not validate_verhoeff(parsed.uid):
            raise ValueError("No valid UID found using Verhoeff check")
        return parsed, full_text


app = FastAPI(title="OCR + Groq Parser API", version="1.0.0")
parser = OCRGroqParser()
storage = StorageManager()
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _extension(filename: str | None) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


def _looks_like_image(data: bytes) -> bool:
    signatures = (
        data.startswith(b"\xff\xd8\xff"),  # jpeg
        data.startswith(b"\x89PNG\r\n\x1a\n"),  # png
        data.startswith(b"RIFF") and b"WEBP" in data[:16],  # webp
    )
    return any(signatures)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/parse")
async def parse_image(file: UploadFile = File(...)) -> JSONResponse:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_media_type",
                "message": f"Allowed content types: {sorted(ALLOWED_CONTENT_TYPES)}",
            },
        )

    ext = _extension(file.filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail={
                "error": "unsupported_file_extension",
                "message": f"Allowed extensions: {sorted(ALLOWED_EXTENSIONS)}",
            },
        )

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_file", "message": "Uploaded file is empty."},
        )

    if len(image_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "file_too_large",
                "message": f"Max upload size is {MAX_UPLOAD_SIZE_BYTES} bytes.",
            },
        )

    if not _looks_like_image(image_bytes):
        raise HTTPException(
            status_code=415,
            detail={
                "error": "invalid_image_signature",
                "message": "File content does not match a valid JPEG/PNG/WEBP image.",
            },
        )

    try:
        result, ocr_text = await run_in_threadpool(parser.run_with_bytes, image_bytes)
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail={"error": "ocr_upstream_error", "message": "Azure OCR request failed."},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "processing_failed", "message": str(exc)},
        ) from exc

    return JSONResponse(
        status_code=200,
        content={
            "status": "success",
            "filename": file.filename,
            "content_type": file.content_type,
            "size_bytes": len(image_bytes),
            "ocr_text": ocr_text,
            "parsed": result.model_dump(),
            "storage_status": storage.save(
                filename=file.filename or "",
                content_type=file.content_type or "",
                size_bytes=len(image_bytes),
                ocr_text=ocr_text,
                parsed=result,
            ),
        },
    )


@app.get("/api/search", response_model=SearchResponse)
async def search_parsed_results(keyword: str, limit: int = 50) -> SearchResponse:
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_limit", "message": "limit must be between 1 and 200."},
        )

    if not storage.available_backends():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "storage_not_configured",
                "message": "No storage backend configured. Set Google Sheets or Supabase credentials.",
            },
        )

    try:
        return await run_in_threadpool(storage.search, keyword, limit)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"error": "invalid_keyword", "message": str(exc)}
        ) from exc


DEMO_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>OCR + Groq Demo</title>
  <style>
    :root { --bg: #f3f5f8; --card: #ffffff; --ink: #10253f; --muted: #4b5d73; --ok: #046307; --bad: #9b111e; --accent: #0b4b8a; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Segoe UI", Tahoma, sans-serif; background: linear-gradient(150deg, #edf4ff, var(--bg)); color: var(--ink); }
    .wrap { max-width: 860px; margin: 48px auto; padding: 0 18px; }
    .card { background: var(--card); border-radius: 14px; padding: 20px; box-shadow: 0 12px 24px rgba(16, 37, 63, 0.08); }
    h1 { margin: 0 0 6px; font-size: 1.5rem; }
    p { margin: 0 0 16px; color: var(--muted); line-height: 1.5; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    button { background: var(--accent); color: #fff; border: 0; border-radius: 10px; padding: 10px 16px; cursor: pointer; font-weight: 600; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .dropzone {
      border: 2px dashed #9bb8dc;
      border-radius: 12px;
      background: #f8fbff;
      padding: 22px;
      text-align: center;
      transition: border-color .18s ease, background .18s ease;
      margin-bottom: 12px;
      cursor: pointer;
    }
    .dropzone.dragover {
      border-color: var(--accent);
      background: #eef5ff;
    }
    .hint { color: var(--muted); font-size: .92rem; margin-bottom: 4px; }
    .file-meta { color: #1f3b57; font-size: .9rem; min-height: 20px; }
    .status { margin-top: 12px; font-weight: 700; }
    pre { margin-top: 14px; max-height: 420px; overflow: auto; background: #f6f8fb; padding: 14px; border-radius: 10px; border: 1px solid #d6deea; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"card\">
      <h1>OCR + Groq Parser Demo</h1>
      <p>Upload JPEG/PNG/WEBP image. Server validates type and size, processes in background, and returns JSON with status code.</p>
      <div id=\"dropzone\" class=\"dropzone\">
        <div class=\"hint\">Drag and drop image here, or click to browse</div>
        <div class=\"file-meta\" id=\"fileMeta\">No file selected</div>
      </div>
      <div class=\"row\">
        <input id=\"fileInput\" class=\"hidden\" type=\"file\" accept=\".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp\" />
        <input id=\"cameraInput\" class=\"hidden\" type=\"file\" accept=\"image/*\" capture=\"environment\" />
        <button id=\"cameraBtn\" type=\"button\">Capture Photo</button>
        <button id=\"processBtn\">Process</button>
      </div>
      <div id=\"status\" class=\"status\">Idle</div>
      <pre id=\"result\">{}</pre>
      <hr style=\"margin:18px 0;border:0;border-top:1px solid #d6deea;\" />
      <h2 style=\"margin:0 0 8px;font-size:1.1rem;\">Search Stored Results</h2>
      <p style=\"margin:0 0 10px;color:var(--muted);\">Find partial keyword matches across stored parsed fields.</p>
      <div class=\"row\">
        <input id=\"searchKeyword\" type=\"text\" placeholder=\"e.g. muskan, 6926, FEMALE\" style=\"flex:1;min-width:240px;padding:10px 12px;border:1px solid #c6d5e8;border-radius:10px;\" />
        <input id=\"searchLimit\" type=\"number\" min=\"1\" max=\"200\" value=\"50\" style=\"width:100px;padding:10px 12px;border:1px solid #c6d5e8;border-radius:10px;\" />
        <button id=\"searchBtn\" type=\"button\">Search</button>
      </div>
      <div id=\"searchStatus\" class=\"status\">Search idle</div>
      <pre id=\"searchResult\">{}</pre>
    </div>
  </div>

  <script>
    const fileInput = document.getElementById('fileInput');
    const cameraInput = document.getElementById('cameraInput');
    const dropzone = document.getElementById('dropzone');
    const fileMeta = document.getElementById('fileMeta');
    const cameraBtn = document.getElementById('cameraBtn');
    const processBtn = document.getElementById('processBtn');
    const statusEl = document.getElementById('status');
    const resultEl = document.getElementById('result');
    const searchKeywordEl = document.getElementById('searchKeyword');
    const searchLimitEl = document.getElementById('searchLimit');
    const searchBtn = document.getElementById('searchBtn');
    const searchStatusEl = document.getElementById('searchStatus');
    const searchResultEl = document.getElementById('searchResult');
    let selectedFile = null;

    function setStatus(text, ok = true) {
      statusEl.textContent = text;
      statusEl.style.color = ok ? 'var(--ok)' : 'var(--bad)';
    }

    function setSearchStatus(text, ok = true) {
      searchStatusEl.textContent = text;
      searchStatusEl.style.color = ok ? 'var(--ok)' : 'var(--bad)';
    }

    function setFile(file) {
      selectedFile = file || null;
      if (!selectedFile) {
        fileMeta.textContent = 'No file selected';
        return;
      }
      const kb = (selectedFile.size / 1024).toFixed(1);
      fileMeta.textContent = `${selectedFile.name} (${kb} KB)`;
    }

    dropzone.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => setFile(fileInput.files?.[0]));
    cameraBtn.addEventListener('click', () => cameraInput.click());
    cameraInput.addEventListener('change', () => setFile(cameraInput.files?.[0]));

    ['dragenter', 'dragover'].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add('dragover');
      });
    });

    ['dragleave', 'drop'].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove('dragover');
      });
    });

    dropzone.addEventListener('drop', (e) => {
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      const dt = new DataTransfer();
      dt.items.add(file);
      fileInput.files = dt.files;
      setFile(file);
    });

    processBtn.addEventListener('click', async () => {
      const file = selectedFile;
      if (!file) {
        setStatus('Choose a file first.', false);
        return;
      }

      processBtn.disabled = true;
      setStatus('Processing on server...', true);
      resultEl.textContent = '{}';

      try {
        const fd = new FormData();
        fd.append('file', file);

        const res = await fetch('/api/parse', { method: 'POST', body: fd });
        const data = await res.json();

        setStatus(`HTTP ${res.status} ${res.ok ? 'Success' : 'Error'}`, res.ok);
        resultEl.textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        setStatus('Network or server error.', false);
        resultEl.textContent = JSON.stringify({ error: String(err) }, null, 2);
      } finally {
        processBtn.disabled = false;
      }
    });

    searchBtn.addEventListener('click', async () => {
      const keyword = (searchKeywordEl.value || '').trim();
      const limit = Number(searchLimitEl.value || 50);

      if (!keyword) {
        setSearchStatus('Enter a keyword first.', false);
        return;
      }
      if (!Number.isFinite(limit) || limit < 1 || limit > 200) {
        setSearchStatus('Limit must be between 1 and 200.', false);
        return;
      }

      searchBtn.disabled = true;
      setSearchStatus('Searching...', true);
      searchResultEl.textContent = '{}';

      try {
        const params = new URLSearchParams({ keyword, limit: String(limit) });
        const res = await fetch(`/api/search?${params.toString()}`, { method: 'GET' });
        const data = await res.json();
        setSearchStatus(`HTTP ${res.status} ${res.ok ? 'Success' : 'Error'}`, res.ok);
        searchResultEl.textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        setSearchStatus('Network or server error.', false);
        searchResultEl.textContent = JSON.stringify({ error: String(err) }, null, 2);
      } finally {
        searchBtn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return DEMO_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
