# OCR + Groq Parser API

Open-source FastAPI service that:
- accepts an uploaded image,
- extracts text using Azure Vision OCR endpoint,
- parses structured identity fields using Groq LLM,
- returns strict JSON with proper HTTP status codes.

This project is free to use and modify under the MIT License.

## Features
- FastAPI backend (`/api/parse`)
- Demo web page (`/demo`) with drag-and-drop upload
- Input hardening:
  - max upload size limit
  - MIME + file extension checks
  - binary image signature checks
- Security headers + CORS support
- Environment-driven configuration

## Parsed Schema
`parsed` in response follows:

```json
{
  "name": "string",
  "issue_date": "string",
  "dob": "string",
  "uid": "string",
  "gender": "string"
}
```

## Requirements
- Python 3.11+
- Groq API key
- Internet access for:
  - Azure Vision demo endpoint
  - Groq API

## Install
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Environment Variables
Copy `.env.example` to `.env` and set values.

```env
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile
MAX_UPLOAD_SIZE_BYTES=5242880
CORS_ALLOW_ORIGINS=*
```

### `MAX_UPLOAD_SIZE_BYTES`
- Type: integer (bytes)
- Default: `5242880` (5 MB)
- Used at runtime in `/api/parse`
- If upload exceeds this limit, API returns `413 Payload Too Large`

### `CORS_ALLOW_ORIGINS`
- Comma-separated origins or `*`
- Example:
  - `CORS_ALLOW_ORIGINS=*`
  - `CORS_ALLOW_ORIGINS=https://app.example.com,https://admin.example.com`

## Run Server
```bash
python app.py
```
Server runs at `http://127.0.0.1:8000`.

## API Endpoints

### 1) Health Check
- **Method:** `GET`
- **Path:** `/health`
- **Response:** `200 OK`

```json
{"status":"ok"}
```

### 2) Demo Page
- **Method:** `GET`
- **Path:** `/demo`
- **Response:** `200 OK` HTML

### 3) Parse Image
- **Method:** `POST`
- **Path:** `/api/parse`
- **Content-Type:** `multipart/form-data`
- **Body field:** `file` (required)

#### Allowed file types
- MIME: `image/jpeg`, `image/png`, `image/webp`
- Extensions: `.jpg`, `.jpeg`, `.png`, `.webp`
- Signature: must match JPEG/PNG/WEBP magic bytes

#### Request headers
Set by client automatically for multipart requests:
- `Content-Type: multipart/form-data; boundary=...`

Optional/common:
- `Accept: application/json`
- `Origin: <your origin>` (for browser CORS)

#### Example request (cURL)
```bash
curl -X POST "http://127.0.0.1:8000/api/parse" \
  -H "Accept: application/json" \
  -F "file=@/absolute/path/to/card.jpeg"
```

#### Success response (`200 OK`)
```json
{
  "status": "success",
  "filename": "card.jpeg",
  "content_type": "image/jpeg",
  "size_bytes": 53360,
  "ocr_text": "...",
  "parsed": {
    "name": "Muskan",
    "issue_date": "20/07/2019",
    "dob": "29/12/2010",
    "uid": "692684826670",
    "gender": "FEMALE"
  }
}
```

#### Error responses
- `400` empty file
- `413` file too large
- `415` unsupported MIME/extension/signature
- `422` OCR returned no text / parsing failed
- `502` upstream OCR service failure

Error body format:
```json
{
  "detail": {
    "error": "error_code",
    "message": "human readable message"
  }
}
```

## CORS
CORS is enabled using `CORS_ALLOW_ORIGINS`.
- `*` allows public browser access from any origin.
- For production, prefer explicit origins.

## Security Notes
- Do **not** commit `.env` to git.
- Rotate API keys if they were ever exposed publicly.
- Keep max upload size small to reduce abuse risk.
- This project validates MIME, extension, and binary signature, but you should still run behind a reverse proxy/WAF in production.
- The Azure endpoint currently used is a demo endpoint and may have throttling/availability limits.

## Open Source Usage
- License: MIT ([LICENSE](LICENSE))
- You can use this code in personal, academic, or commercial projects.
- Attribution is appreciated but not required by MIT.

## Project Structure
- `app.py` - FastAPI server + demo UI + OCR/Groq pipeline
- `requirements.txt` - Python dependencies
- `.env.example` - environment variable template
- `.gitignore` - local/secrets ignore rules
- `LICENSE` - MIT license

## Quick Test in Browser
1. Start server: `python app.py`
2. Open `http://127.0.0.1:8000/demo`
3. Drag/drop or browse image
4. Click `Process`
5. View JSON response and status
