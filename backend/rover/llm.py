"""OpenRouter call (project rule: OpenRouter ONLY, never a direct vendor SDK).
Shared by the vision fleet and the challenger. Retries + 429 backoff."""
import base64
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, "/app")
import config  # noqa: E402  (loads OPENROUTER_API_KEY)

URL = "https://openrouter.ai/api/v1/chat/completions"

# The primary reader MUST be PDF-capable (OpenRouter 'file' input modality) — it is
# handed the raw PDF by llm.pdf_content(), not page images. The default is kept in
# step with docker-compose so a local run and the container agree; they had drifted
# to three different models (compose 3.5-flash / here grok-4.5 / the UAT harness
# 3-flash-preview), which makes any accuracy or cost comparison meaningless.
# grok-4.5 is IMAGE-ONLY and must never be the primary — it stays the challenger.
PRIMARY = os.environ.get("ROVER_PRIMARY_MODEL", "google/gemini-3.6-flash")
CHALLENGER = os.environ.get("ROVER_CHALLENGER_MODEL", "x-ai/grok-4.5")


def pdf_content(pdf_path: str) -> list:
    """Native-PDF content block for PDF-capable models (Gemini / Claude on
    OpenRouter). The model reads the doc's text layer directly (no OCR of a
    downscaled JPEG) — higher accuracy + far fewer tokens than page-images.
    Image-only models (grok) must NOT be fed this; use router.image_content."""
    b64 = base64.b64encode(open(pdf_path, "rb").read()).decode()
    return [{"type": "file",
             "file": {"filename": "doc.pdf",
                      "file_data": f"data:application/pdf;base64,{b64}"}}]


def call(model: str, prompt: str, image_content: list, max_tokens: int = 3000):
    """Return (parsed_json_or_None, raw_text, usage, err)."""
    content = [{"type": "text", "text": prompt}] + image_content
    payload = {"model": model,
               "messages": [{"role": "user", "content": content}],
               "temperature": 0, "max_tokens": max_tokens}
    resp = None
    for attempt in range(5):
        try:
            resp = requests.post(
                URL,
                headers={"Authorization": f"Bearer {config.API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=300)
            if resp.status_code == 429:
                time.sleep(4 * (attempt + 1)); continue
            break
        except requests.exceptions.RequestException:
            time.sleep(4 * (attempt + 1))
    if resp is None:
        return None, "", {}, "timeout"
    if resp.status_code != 200:
        return None, "", {}, f"http {resp.status_code}: {resp.text[:200]}"
    try:
        j = resp.json()
        choice = j["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return None, "", {}, f"bad response: {type(e).__name__} {str(resp.text)[:160]}"
    if content is None:
        # A 200 with a null content block. Seen for real on a 12-page bundle: the
        # model hit its output cap and returned nothing rather than truncated
        # JSON. `.strip()` on it raised AttributeError, which was NOT in the
        # except tuple above, so it escaped as a crash and killed the whole
        # batch mid-run. Report it like any other failed read and let the caller
        # carry on with the remaining documents.
        reason = choice.get("finish_reason") or choice.get("native_finish_reason")
        return None, "", (j.get("usage") or {}), f"empty response (finish_reason={reason})"
    raw = content.strip()
    cleaned = re.sub(r"```json\n?|```\n?", "", raw).strip()
    parsed = None
    try:
        parsed = json.loads(cleaned[cleaned.index("{"):cleaned.rindex("}") + 1])
    except Exception:
        pass
    return parsed, raw, (j.get("usage") or {}), None
