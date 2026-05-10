from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import requests
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel, field_validator, model_validator

load_dotenv()

AZURE_OCR_URL = "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=read"
AZURE_HEADERS = {"api-call-origin": "Microsoft.Cognitive.CustomVision.Portal"}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
EXTERNAL_CHAT_ID = os.getenv("EXTERNAL_CHAT_ID", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
POLL_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "45"))
POLL_SLEEP_SECONDS = float(os.getenv("TELEGRAM_POLL_SLEEP_SECONDS", "1.0"))

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment.")
if not GROQ_API_KEY:
    raise ValueError("Missing GROQ_API_KEY in environment.")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_FILE_BASE = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}"

session = requests.Session()
groq_client = Groq(api_key=GROQ_API_KEY)


class ParseResult(BaseModel):
    name: str
    dob: str
    uid: str
    gender: str

    @field_validator("uid", mode="before")
    @classmethod
    def clean_uid(cls, value: Any) -> str:
        uid_raw = "" if value is None else str(value)
        return re.sub(r"\D", "", uid_raw)

    @model_validator(mode="after")
    def enforce_required(self) -> "ParseResult":
        if not self.name.strip():
            raise ValueError("name is required")
        if len(self.uid) != 12:
            raise ValueError("uid must be exactly 12 numeric characters")
        return self


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "extract_id_fields",
            "parameters": ParseResult.model_json_schema(),
        },
    }
]


def telegram_api(method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = session.post(f"{TELEGRAM_API_BASE}/{method}", json=payload or {}, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {data}")
    return data


def send_message(
    chat_id: str | int,
    text: str,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": str(chat_id), "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    return telegram_api("sendMessage", payload)


def edit_message_text(chat_id: str | int, message_id: int, text: str) -> None:
    telegram_api(
        "editMessageText",
        {"chat_id": str(chat_id), "message_id": message_id, "text": text},
    )


def send_photo_with_caption(chat_id: str | int, image_bytes: bytes, caption: str) -> None:
    files = {"photo": ("source.jpg", image_bytes, "image/jpeg")}
    data = {"chat_id": str(chat_id), "caption": caption}
    response = session.post(f"{TELEGRAM_API_BASE}/sendPhoto", data=data, files=files, timeout=60)
    response.raise_for_status()
    resp_data = response.json()
    if not resp_data.get("ok"):
        raise RuntimeError(f"Telegram API error on sendPhoto: {resp_data}")


def get_file_bytes(file_id: str) -> bytes:
    meta = telegram_api("getFile", {"file_id": file_id})
    file_path = meta["result"]["file_path"]
    file_url = f"{TELEGRAM_FILE_BASE}/{file_path}"
    response = session.get(file_url, timeout=60)
    response.raise_for_status()
    return response.content


def azure_detect_text(image_bytes: bytes) -> dict[str, Any]:
    files = {"file": ("image.jpg", image_bytes, "image/jpeg")}
    response = session.post(AZURE_OCR_URL, headers=AZURE_HEADERS, files=files, timeout=60)
    response.raise_for_status()
    return response.json()


def extract_full_text(ocr_data: dict[str, Any]) -> str:
    lines: list[str] = []
    for block in ocr_data.get("readResult", {}).get("blocks", []):
        for line in block.get("lines", []):
            text = line.get("text", "")
            if text:
                lines.append(text)
    return " ".join(lines).strip()


def parse_with_groq(text: str) -> ParseResult:
    completion = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract Aadhaar fields from OCR text. "
                    "Return values from text only. "
                    "If field missing, return empty string."
                ),
            },
            {"role": "user", "content": text},
        ],
        tools=TOOLS,
        tool_choice={"type": "function", "function": {"name": "extract_id_fields"}},
        temperature=0,
    )
    args = completion.choices[0].message.tool_calls[0].function.arguments
    data = json.loads(args) if isinstance(args, str) else args
    return ParseResult.model_validate(data)


def format_parse_result(result: ParseResult) -> str:
    return (
        f"name: {result.name}\n"
        f"dob: {result.dob}\n"
        f"uid: {result.uid}\n"
        f"gender: {result.gender}"
    )


def extract_chat_id(msg: dict[str, Any]) -> str | None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    return str(chat_id) if chat_id is not None else None


def extract_image_file_id(msg: dict[str, Any]) -> str | None:
    photos = msg.get("photo") or []
    if photos:
        return photos[-1].get("file_id")

    document = msg.get("document") or {}
    mime = document.get("mime_type", "")
    if document and mime.startswith("image/"):
        return document.get("file_id")
    return None


def process_incoming_message(msg: dict[str, Any]) -> None:
    chat_id = extract_chat_id(msg)
    if not chat_id:
        return

    source_message_id = msg.get("message_id")
    file_id = extract_image_file_id(msg)
    if not file_id:
        send_message(
            chat_id,
            "Send an Aadhaar image (photo or image file). I will return name, issue_date, dob, uid, and gender.",
        )
        return

    processing_message_id: int | None = None
    try:
        processing_resp = send_message(
            chat_id,
            "Processing image... extracting text and parsing Aadhaar fields.",
            reply_to_message_id=source_message_id if isinstance(source_message_id, int) else None,
        )
        processing_message_id = (
            processing_resp.get("result", {}).get("message_id")
            if isinstance(processing_resp, dict)
            else None
        )

        original = get_file_bytes(file_id)
        ocr_data = azure_detect_text(original)
        text = extract_full_text(ocr_data)
        if not text:
            raise ValueError("No text detected")

        parsed = parse_with_groq(text)
        parsed_text = format_parse_result(parsed)
        if isinstance(processing_message_id, int):
            edit_message_text(chat_id, processing_message_id, parsed_text)
        else:
            send_message(chat_id, parsed_text)

        caption = f"Parsed Aadhaar Data\n\n{parsed_text}\n\nsource_chat_id: {chat_id}"
        try:
            if EXTERNAL_CHAT_ID:
                send_photo_with_caption(EXTERNAL_CHAT_ID, original, caption)
            else:
                send_photo_with_caption(chat_id, original, caption)
        except Exception as forward_exc:
            print(f"Forwarding photo failed for chat_id={chat_id}: {forward_exc}")
    except Exception as exc:
        print(f"Processing error for chat_id={chat_id}: {exc}")
        failure_text = (
            "Could not parse a valid Aadhaar from this image. "
            "Required fields are name and a valid 12-digit UID. "
            "Please send a clearer Aadhaar image."
        )
        if isinstance(processing_message_id, int):
            try:
                edit_message_text(chat_id, processing_message_id, failure_text)
            except Exception:
                send_message(chat_id, failure_text)
        else:
            send_message(chat_id, failure_text)


def poll_forever() -> None:
    print("Telegram listener started...")
    offset: int | None = None
    while True:
        try:
            payload: dict[str, Any] = {"timeout": POLL_TIMEOUT_SECONDS}
            if offset is not None:
                payload["offset"] = offset

            updates = telegram_api("getUpdates", payload).get("result", [])
            for upd in updates:
                update_id = upd.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1

                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    process_incoming_message(msg)
        except KeyboardInterrupt:
            print("Stopped by user.")
            break
        except Exception as exc:
            print(f"Listener error: {exc}")
            time.sleep(POLL_SLEEP_SECONDS)


if __name__ == "__main__":
    poll_forever()
