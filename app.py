from __future__ import annotations

import base64
import json
import os
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from groq import Groq
from pydantic import BaseModel

load_dotenv()

AZURE_OCR_URL = "https://portal.vision.cognitive.azure.com/api/demo/analyze?features=read"
HEADERS = {"api-call-origin": "Microsoft.Cognitive.CustomVision.Portal"}

MAX_UPLOAD_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(5 * 1024 * 1024)))
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]


class ParseResult(BaseModel):
    name: str
    issue_date: str
    dob: str
    uid: str
    gender: str


class OCRGroqParser:
    def __init__(self) -> None:
        self.session = requests.Session()

        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("Missing GROQ_API_KEY in environment.")

        self.model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.client = Groq(api_key=api_key)
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": "extract_id_fields",
                    "parameters": ParseResult.model_json_schema(),
                },
            }
        ]

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

    def detect_text(self, image_bytes: bytes) -> dict[str, Any]:
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

    def parse_with_groq(self, text: str) -> ParseResult:
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract the requested Aadhaar-like identity fields from OCR text. "
                        "Return only values from text; if missing, use empty string."
                    ),
                },
                {"role": "user", "content": text},
            ],
            tools=self.tools,
            tool_choice={"type": "function", "function": {"name": "extract_id_fields"}},
            temperature=0,
        )

        args = completion.choices[0].message.tool_calls[0].function.arguments
        data = json.loads(args) if isinstance(args, str) else args
        return ParseResult.model_validate(data)

    def run_with_bytes(self, image_bytes: bytes) -> tuple[ParseResult, str]:
        ocr_result = self.detect_text(image_bytes)
        full_text = self.extract_full_text(ocr_result)
        if not full_text:
            raise ValueError("No text detected by OCR.")
        return self.parse_with_groq(full_text), full_text


app = FastAPI(title="OCR + Groq Parser API", version="1.0.0")
parser = OCRGroqParser()
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
        },
    )


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
        <button id=\"processBtn\">Process</button>
      </div>
      <div id=\"status\" class=\"status\">Idle</div>
      <pre id=\"result\">{}</pre>
    </div>
  </div>

  <script>
    const fileInput = document.getElementById('fileInput');
    const dropzone = document.getElementById('dropzone');
    const fileMeta = document.getElementById('fileMeta');
    const processBtn = document.getElementById('processBtn');
    const statusEl = document.getElementById('status');
    const resultEl = document.getElementById('result');
    let selectedFile = null;

    function setStatus(text, ok = true) {
      statusEl.textContent = text;
      statusEl.style.color = ok ? 'var(--ok)' : 'var(--bad)';
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
  </script>
</body>
</html>
"""


@app.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return DEMO_HTML


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
