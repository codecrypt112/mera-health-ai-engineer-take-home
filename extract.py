"""Extract structured lab results from a lab report.

Usage:
    python extract.py reports/report_01.pdf

Prints JSON matching schema.json to stdout.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Model substitution: the kit's default gemini-3.8-flash returned persistent 503s (provider
# overload) when this was built; gemini-3.5-flash-lite is the available replacement in the
# same tier. See README.md "Provider substitution" for the measured-before-changing-behavior
# note.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
DPI = 60

PROMPT = """You are an expert at reading medical lab reports.

Look at this lab report and extract every test result from it. If more than one page
image is given, they are pages of the same report - extract tests from every page.

Return JSON in exactly this format:
{"tests": [{"test_name": "...", "value": 0.0, "unit": "...", "reference_range": "...", "status": "normal"}]}

Rules:
- Use the standard medical name for each test.
- value is the result exactly as printed: a plain number as a number, otherwise the exact
  text (e.g. a range like "2-3", a qualitative result like "Negative", a description like
  "Dark Yellow"). Never collapse a range into a single average number.
- reference_range is exactly as printed on the report. If the report does not print one for
  a test, use null - do not invent a "standard" range; different labs and methods use
  different ranges, and a wrong one is misleading.
- status is "normal", "high" or "low" based on the value and the reference range, or null if
  the value is not comparable to a range (e.g. a qualitative or descriptive result).
- Be thorough. Do not miss any tests.
"""

MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def load_images(path: Path) -> list[tuple[str, str]]:
    """Return [(media_type, base64 data), ...] - one entry per page for a PDF."""
    if path.suffix.lower() == ".pdf":
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "page"
            try:
                subprocess.run(["pdftoppm", "-r", str(DPI), "-png", str(path), str(prefix)],
                                check=True, capture_output=True)
            except FileNotFoundError:
                raise RuntimeError("Install Poppler (pdftoppm); see README.md.") from None
            pages = sorted(Path(directory).glob("page-*.png"),
                            key=lambda p: int(re.search(r"-(\d+)\.png$", p.name).group(1)))
            return [("image/png", base64.standard_b64encode(p.read_bytes()).decode()) for p in pages]
    media_type = MEDIA_TYPES[path.suffix.lower()]
    return [(media_type, base64.standard_b64encode(path.read_bytes()).decode())]


def call_model(images: list[tuple[str, str]]) -> tuple[str, dict]:
    """Return (response_text, usage) for one extraction call over one or more page images."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("Set GEMINI_API_KEY; see README.md. No API call made.")
    image_parts = [{"inlineData": {"mimeType": mt, "data": d}} for mt, d in images]
    body = {
        "contents": [{"role": "user", "parts": image_parts + [{"text": PROMPT}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 16000},
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
        data=json.dumps(body).encode(), headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Gemini HTTP {error.code}; check API access, model and quota. See README.md.") from None
    except urllib.error.URLError:
        raise RuntimeError("Gemini connection failed; check network/TLS access and retry. See README.md.") from None
    candidate = (result.get("candidates") or [{}])[0]
    finish_reason = candidate.get("finishReason")
    if finish_reason and finish_reason != "STOP":
        print(f"WARNING: model finishReason={finish_reason} "
              "(response may be truncated or blocked)", file=sys.stderr)
    text = "".join(part["text"] for part in candidate.get("content", {}).get("parts", [])
                   if "text" in part and not part.get("thought"))
    return text, (result.get("usageMetadata") or {})


def log_usage(usage: dict, latency_s: float) -> None:
    """Emit one line of cost/latency telemetry to stderr. stdout stays pure schema JSON."""
    record = {
        "model": MODEL,
        "latency_s": round(latency_s, 3),
        "prompt_tokens": usage.get("promptTokenCount"),
        "completion_tokens": usage.get("candidatesTokenCount"),
        "n_pages": usage.get("n_pages"),
    }
    print(json.dumps(record), file=sys.stderr)


def parse(text: str) -> list[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(match.group(0))["tests"]
    except Exception:
        if text.strip():
            print(f"WARNING: could not parse model output as the expected JSON "
                  f"({len(text)} chars received) - returning zero tests", file=sys.stderr)
        return []


def clean_value(value):
    """Numbers stay numbers; anything else (a range, a qualitative result, text) is kept as
    printed rather than discarded - schema.json allows value to be a string."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass in Python - schema has no boolean
        return str(value)
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text.replace(",", ""))  # handles Indian-style "2,29,000" grouping
    except ValueError:
        return text


def clean(tests: list[dict]) -> list[dict]:
    out = []
    for t in tests:
        out.append({
            "test_name": str(t.get("test_name", "")).strip(),
            "value": clean_value(t.get("value")),
            "unit": t.get("unit") or None,
            "reference_range": t.get("reference_range") or None,
            "status": t.get("status"),
        })
    return out


def extract(path: Path) -> dict:
    images = load_images(path)
    start = time.monotonic()
    text, usage = call_model(images)
    usage["n_pages"] = len(images)
    log_usage(usage, time.monotonic() - start)
    return {"tests": clean(parse(text))}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        print(json.dumps(extract(Path(sys.argv[1])), indent=2, ensure_ascii=False))
    except RuntimeError as error:
        sys.exit(str(error))
