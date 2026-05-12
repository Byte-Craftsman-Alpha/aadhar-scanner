from __future__ import annotations

import json
import logging
import os
import re
import time
from io import BytesIO
from typing import Any

import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps
from pydantic import BaseModel
import threading
try:
    from supabase import Client as SupabaseClient
    from supabase import create_client as create_supabase_client
except Exception:
    SupabaseClient = None
    create_supabase_client = None

# Create a global event
stop_event = threading.Event()
    
load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("aadhar_scanner.telegram")

# =========================================================
# AZURE
# =========================================================

AZURE_OCR_URL = (
    "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=read"
)

AZURE_CAPTION_URL = (
    "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=denseCaptions"
)

AZURE_HEADERS = {
    "api-call-origin": "Microsoft.Cognitive.CustomVision.Portal"
}

# =========================================================
# TELEGRAM CONFIG
# =========================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
EXTERNAL_CHAT_ID = os.getenv("EXTERNAL_CHAT_ID", "").strip()

POLL_TIMEOUT_SECONDS = int(
    os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "45")
)

POLL_SLEEP_SECONDS = float(
    os.getenv("TELEGRAM_POLL_SLEEP_SECONDS", "1.0")
)

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment.")

TELEGRAM_API_BASE = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
)

TELEGRAM_FILE_BASE = (
    f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"
)

session = requests.Session()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    or os.getenv("SUPABASE_KEY", "").strip()
)
SUPABASE_PARSE_TABLE = os.getenv("SUPABASE_PARSE_TABLE", "aadhaar_parsed").strip()


def init_supabase_client() -> SupabaseClient | None:
    if not create_supabase_client:
        logger.warning("Supabase package not available.")
        return None
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("Supabase credentials missing; parse result will not be stored.")
        return None
    try:
        return create_supabase_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as exc:
        logger.exception("Supabase client initialization failed: %s", exc)
        return None


def ensure_parse_table(client: SupabaseClient | None) -> bool:
    if client is None:
        return False

    ddl = f"""
    create table if not exists public.{SUPABASE_PARSE_TABLE} (
        name text not null,
        dob text not null,
        uid text not null unique,
        gender text not null
    );
    """
    try:
        client.rpc("exec_sql", {"sql": ddl}).execute()
        logger.info("Supabase parse table ensured: %s", SUPABASE_PARSE_TABLE)
        return True
    except Exception as exc:
        logger.error(
            "Unable to create/verify Supabase parse table '%s'. "
            "Create an RPC function named 'exec_sql' or create table manually. Error: %s",
            SUPABASE_PARSE_TABLE,
            exc,
        )
        return False


supabase_client = init_supabase_client()
supabase_parse_table_ready = ensure_parse_table(supabase_client)

# =========================================================
# MODELS
# =========================================================


class ParseResult(BaseModel):
    name: str
    dob: str
    uid: str
    gender: str


def save_parse_result_to_supabase(parsed: ParseResult) -> None:
    if supabase_client is None:
        logger.error("Supabase save skipped: client not initialized.")
        return
    if not supabase_parse_table_ready:
        logger.error("Supabase save skipped: parse table is not ready.")
        return

    payload = {
        "name": parsed.name.strip(),
        "dob": parsed.dob.strip(),
        "uid": parsed.uid.strip(),
        "gender": parsed.gender.strip(),
    }

    missing = [k for k, v in payload.items() if not v]
    if missing:
        logger.error(
            "Supabase save aborted due to missing required fields: %s",
            ",".join(missing),
        )
        return

    try:
        supabase_client.table(SUPABASE_PARSE_TABLE).upsert(
            payload,
            on_conflict="uid",
        ).execute()
        logger.info("Supabase parse result saved (uid=%s).", payload["uid"])
    except Exception as exc:
        logger.exception(
            "Supabase save failed for parsed result (uid=%s): %s",
            payload["uid"],
            exc,
        )


# =========================================================
# VERHOEFF ALGORITHM
# =========================================================

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


def validate_verhoeff(number: str):
    try:
        if not number.isdigit() or len(number) != 12:
            return False

        if number[0] in ["0", "1"]:
            return False

        c = 0

        for i, item in enumerate(reversed(number)):
            c = _v_multiplication[c][_v_permutation[i % 8][int(item)]]

        return c == 0

    except Exception:
        return False


def find_valid_substrings(large_string: str):
    valid_matches = []

    if len(large_string) < 12:
        return valid_matches

    for i in range(len(large_string) - 11):
        candidate = large_string[i : i + 12]

        if validate_verhoeff(candidate):
            valid_matches.append(candidate)

    return valid_matches


def extract_valid_uid_from_text(text: str) -> str:
    digit_stream = re.sub(r"\D", "", text or "")
    matches = find_valid_substrings(digit_stream)
    return matches[0] if matches else ""


# =========================================================
# TELEGRAM HELPERS
# =========================================================

def telegram_api(
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:

    response = session.post(
        f"{TELEGRAM_API_BASE}/{method}",
        json=payload or {},
        timeout=60,
    )

    if response.status_code != 200:
        print(
            f"Telegram API Error: "
            f"{response.status_code} - {response.text}"
        )

    response.raise_for_status()

    return response.json()


def send_message(
    chat_id: str | int,
    text: str,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:

    payload: dict[str, Any] = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "Markdown",
    }

    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    return telegram_api("sendMessage", payload)


def edit_message_text(
    chat_id: str | int,
    message_id: int,
    text: str,
) -> None:

    telegram_api(
        "editMessageText",
        {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
        },
    )


def send_photo_with_caption(
    chat_id: str | int,
    image_bytes: bytes,
    caption: str,
) -> None:

    files = {
        "photo": ("source.jpg", image_bytes, "image/jpeg")
    }

    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "Markdown",
    }

    response = session.post(
        f"{TELEGRAM_API_BASE}/sendPhoto",
        data=data,
        files=files,
        timeout=60,
    )

    response.raise_for_status()

    resp_data = response.json()

    if not resp_data.get("ok"):
        raise RuntimeError(
            f"Telegram sendPhoto error: {resp_data}"
        )


def get_file_bytes(file_id: str) -> bytes:
    meta = telegram_api(
        "getFile",
        {"file_id": file_id},
    )

    file_path = meta["result"]["file_path"]

    file_url = f"{TELEGRAM_FILE_BASE}/{file_path}"

    response = session.get(file_url, timeout=60)

    response.raise_for_status()

    return response.content


# =========================================================
# CARD DETECTION
# =========================================================

def detect_card(image_bytes: bytes):
    files = {
        "file": ("image.jpg", image_bytes, "image/jpeg")
    }

    response = session.post(
        AZURE_CAPTION_URL,
        headers=AZURE_HEADERS,
        files=files,
        timeout=60,
    )

    response.raise_for_status()

    data = response.json()

    print(json.dumps(data, indent=2))

    values = data.get(
        "denseCaptionsResult",
        {},
    ).get("values", [])

    matches = []

    for item in values:
        text = item.get("text", "").lower()

        print("FOUND:", text)

        if (
            "id card" in text
            or "identity card" in text
            or "card" in text
            or "document" in text
        ):
            box = item.get("boundingBox", {})

            w = box.get("w", 0)
            h = box.get("h", 0)

            area = w * h

            matches.append({
                "item": item,
                "area": area,
            })

    if not matches:
        return None

    best_match = min(
        matches,
        key=lambda x: x["area"],
    )

    print("SELECTED:", best_match["item"])

    return best_match["item"]["boundingBox"]


def crop_image_bytes(
    image_bytes,
    x,
    y,
    w,
    h,
):
    img = Image.open(BytesIO(image_bytes))

    img = ImageOps.exif_transpose(img)

    print("IMAGE SIZE:", img.size)
    print("CROP:", x, y, w, h)

    img_width, img_height = img.size

    x = max(0, int(x))
    y = max(0, int(y))

    right = min(
        x + int(w),
        img_width,
    )

    bottom = min(
        y + int(h),
        img_height,
    )

    cropped = img.crop(
        (x, y, right, bottom)
    )

    output = BytesIO()

    cropped.save(output, format="JPEG")

    return output.getvalue()


# =========================================================
# OCR
# =========================================================

def azure_detect_text(
    image_bytes: bytes,
) -> tuple[dict[str, Any], bytes]:

    cropped_image = image_bytes

    try:
        box = detect_card(image_bytes)

        if box:
            cropped_image = crop_image_bytes(
                image_bytes,
                x=box["x"],
                y=box["y"],
                w=box["w"],
                h=box["h"],
            )

            print("Card cropped successfully.")

        else:
            print(
                "No card detected. "
                "Using original image."
            )

    except Exception as crop_exc:
        print(
            f"Card detection/cropping failed: "
            f"{crop_exc}"
        )

    files = {
        "file": ("image.jpg", cropped_image, "image/jpeg")
    }

    response = session.post(
        AZURE_OCR_URL,
        headers=AZURE_HEADERS,
        files=files,
        timeout=60,
    )

    response.raise_for_status()

    return response.json(), cropped_image


def extract_full_text(
    ocr_data: dict[str, Any],
) -> str:

    lines: list[str] = []

    for block in ocr_data.get(
        "readResult",
        {},
    ).get("blocks", []):

        for line in block.get("lines", []):
            text = line.get("text", "")

            if text:
                lines.append(text)

    return " ".join(lines).strip()


# =========================================================
# REGEX PARSER
# =========================================================

def parse_with_regex(text: str) -> ParseResult:
    name = ""
    dob = ""
    uid = ""
    gender = ""

    name_match = re.search(
        r"[I|1|l]ndia[^a-zA-Z]*([a-zA-Z\s]+?)[^a-zA-Z]*D[O|0|o]B",
        text,
        flags=re.DOTALL,
    )

    if name_match:
        name = name_match.group(1).strip()

    dob_match = re.search(
        r"D[O|0|o]B[:\s]+(\d{2}\/\d{2}\/\d{4})",
        text,
        flags=re.IGNORECASE,
    )

    if dob_match:
        dob = dob_match.group(1).strip()

    uid_match = re.search(
        r"((?<=\s)\d{4}\s(?<=\s)\d{4}\s(?<=\s)\d{4})(?:\sVID\s*:\s*\d{4}\s\d{4}\s\d{4}\s\d{4})?",
        text,
        flags=re.IGNORECASE,
    )
    if uid_match:
        print(text)
        uid = re.sub(r"\D", "", uid_match.group(1))
        print(f"Detected UID: {uid}")

    uid = extract_valid_uid_from_text(uid)

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


# =========================================================
# FORMATTERS
# =========================================================

def escape_markdown(text: str) -> str:
    parse_chars = r"_*`["

    return re.sub(
        f"([{re.escape(parse_chars)}])",
        r"\\\1",
        text,
    )


def format_parse_result(
    result: ParseResult,
) -> str:

    safe_name = escape_markdown(result.name)
    safe_gender = escape_markdown(result.gender)

    return (
        f"*Name:* *{safe_name}*\n"
        f"*Date of Birth:* {result.dob}\n"
        f"*Gender:* {safe_gender}\n"
        f"*Aadhaar No:* `{result.uid}`\n"
    )


# =========================================================
# MESSAGE HELPERS
# =========================================================

def extract_chat_id(
    msg: dict[str, Any],
) -> str | None:

    chat = msg.get("chat") or {}

    chat_id = chat.get("id")

    return (
        str(chat_id)
        if chat_id is not None
        else None
    )


def extract_username(
    msg: dict[str, Any],
) -> str:

    user = msg.get("from") or {}

    username = user.get("username")

    if username:
        return f"@{username}"

    return (
        user.get("first_name")
        or "Unknown"
    )


def extract_image_file_id(
    msg: dict[str, Any],
) -> str | None:

    photos = msg.get("photo") or []

    if photos:
        return photos[-1].get("file_id")

    document = msg.get("document") or {}

    mime = document.get("mime_type", "")

    if document and mime.startswith("image/"):
        return document.get("file_id")

    return None


# =========================================================
# MAIN PROCESSOR
# =========================================================

def process_incoming_message(
    msg: dict[str, Any],
) -> None:

    chat_id = extract_chat_id(msg)

    if not chat_id:
        return

    sender_name = extract_username(msg)

    source_message_id = msg.get("message_id")

    file_id = extract_image_file_id(msg)

    if not file_id:
        send_message(
            chat_id,
            (
                f"Hi {escape_markdown(sender_name)},\n"
                "Send me an Aadhaar image."
            ),
        )
        return

    processing_message_id: int | None = None

    try:
        # =====================================================
        # STEP 1
        # =====================================================

        processing_resp = send_message(
            chat_id,
            (
                f"Hi {escape_markdown(sender_name)},\n\n"
                "📥 Image received\n"
                "🔍 Detecting Aadhaar card..."
            ),
            reply_to_message_id=source_message_id,
        )

        processing_message_id = (
            processing_resp.get(
                "result",
                {},
            ).get("message_id")
            if isinstance(processing_resp, dict)
            else None
        )

        # =====================================================
        # STEP 2
        # =====================================================

        original = get_file_bytes(file_id)

        if isinstance(processing_message_id, int):
            edit_message_text(
                chat_id,
                processing_message_id,
                (
                    f"Hi {escape_markdown(sender_name)},\n\n"
                    "📥 Image received\n"
                    "✅ Card detected\n"
                    "✂️ Cropping card region..."
                ),
            )

        # =====================================================
        # STEP 3
        # =====================================================

        if isinstance(processing_message_id, int):
            edit_message_text(
                chat_id,
                processing_message_id,
                (
                    f"Hi {escape_markdown(sender_name)},\n\n"
                    "📥 Image received\n"
                    "✅ Card cropped\n"
                    "🧠 Running OCR..."
                ),
            )

        ocr_data, processed_image = azure_detect_text(
            original
        )

        # =====================================================
        # STEP 4
        # =====================================================

        if isinstance(processing_message_id, int):
            edit_message_text(
                chat_id,
                processing_message_id,
                (
                    f"Hi {escape_markdown(sender_name)},\n\n"
                    "📥 Image received\n"
                    "✅ OCR completed\n"
                    "🔎 Extracting Aadhaar details..."
                ),
            )

        text = extract_full_text(ocr_data)

        if not text:
            raise ValueError("No text detected")

        parsed = parse_with_regex(text)

        if not validate_verhoeff(parsed.uid):
            raise ValueError(
                "No valid Aadhaar number found"
            )

        save_parse_result_to_supabase(parsed)

        parsed_text = format_parse_result(parsed)

        # =====================================================
        # FINAL RESPONSE
        # =====================================================

        final_text = (
            "✅ *Aadhaar Parsed Successfully*\n\n"
            f"{parsed_text}"
        )

        if isinstance(processing_message_id, int):
            edit_message_text(
                chat_id,
                processing_message_id,
                final_text,
            )

        else:
            send_message(
                chat_id,
                final_text,
            )

        # =====================================================
        # ADMIN FORWARD
        # =====================================================

        caption = (
            f"*Parsed Aadhaar Data*\n\n"
            f"{parsed_text}\n"
            f"*Sent by:* "
            f"{escape_markdown(sender_name)}"
        )

        try:
            target_chat = (
                EXTERNAL_CHAT_ID
                if EXTERNAL_CHAT_ID
                else chat_id
            )

            send_photo_with_caption(
                target_chat,
                processed_image,
                caption,
            )

        except Exception as forward_exc:
            print(
                f"Forwarding failed: "
                f"{forward_exc}"
            )

    except Exception as exc:
        print(f"Processing error: {exc}")

        failure_text = (
            "❌ Could not parse a valid Aadhaar.\n\n"
            "A valid Aadhaar number could not "
            "be detected using Verhoeff validation.\n"
            "Please send a clearer Aadhaar image."
        )

        if isinstance(processing_message_id, int):
            try:
                edit_message_text(
                    chat_id,
                    processing_message_id,
                    failure_text,
                )

            except Exception:
                send_message(
                    chat_id,
                    failure_text,
                )

        else:
            send_message(
                chat_id,
                failure_text,
            )


# =========================================================
# STARTUP
# =========================================================

def notify_admin_on_startup():
    if EXTERNAL_CHAT_ID:
        timestamp = time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        msg = (
            f"*Server Startup Success*\n"
            f"*Time:* `{timestamp}`\n"
            f"*Status:* Aadhaar Scanner "
            f"is now polling..."
        )

        try:
            send_message(
                EXTERNAL_CHAT_ID,
                msg,
            )

            print(
                "Startup notification sent."
            )

        except Exception as e:
            print(
                f"Startup notification failed: "
                f"{e}"
            )


# =========================================================
# POLLING LOOP
# =========================================================

def poll_forever() -> None:
    notify_admin_on_startup()
    print("Telegram listener started.")
    offset = None

    # 2. Check the signal instead of 'True'
    while not stop_event.is_set():
        try:
            params = {
                "timeout": POLL_TIMEOUT_SECONDS,
                "offset": offset,
            }

            response = session.get(
                f"{TELEGRAM_API_BASE}/getUpdates",
                params=params,
                # The timeout here is the key!
                timeout=POLL_TIMEOUT_SECONDS + 2, 
            )

            response.raise_for_status()
            data = response.json()
            updates = data.get("result", [])

            for upd in updates:
                offset = upd.get("update_id") + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    process_incoming_message(msg)

        except requests.exceptions.ReadTimeout:
            # This is actually good! It means the loop 
            # refreshes and checks 'stop_event' again.
            continue
        except Exception as exc:
            if stop_event.is_set():
                break # Exit quietly if we are shutting down
            print(f"Critical listener error: {exc}")
            time.sleep(POLL_SLEEP_SECONDS)
    
    print("Telegram listener stopped cleanly.")


# =========================================================
# ENTRYPOINT
# =========================================================

if __name__ == "__main__":
    poll_forever()
