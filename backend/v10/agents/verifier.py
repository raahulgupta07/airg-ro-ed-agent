"""V9 — Cross-check verifier agent (Sonnet).
Reads PDF + draft JSON, returns ONLY the corrections (fields where the draft
disagreed with the PDF after Sonnet's independent re-read).
"""
import base64
import json
import re
from pathlib import Path
from typing import Dict, Optional

from agno.agent import Agent
from agno.models.openrouter import OpenRouter

from v10.config import VERIFIER_MODEL, OPENROUTER_API_KEY


VERIFIER_SYSTEM = """You are a Myanmar Customs verification agent. You re-read
a PDF independently of an existing draft extraction and report only fields
where your reading differs from the draft."""


VERIFIER_PROMPT_TMPL = """Below is a DRAFT extraction of this same PDF.
Independently re-read the PDF.

For each field where your reading differs, return correction.
For each field where you agree, do NOT include it.

Schema for output:
{{
  "corrections": {{
    "declaration.<field>": {{"value": "...", "confidence": "high|med|low", "reason": "..."}},
    "items[<idx>].<field>": {{"value": "...", "confidence": "high|med|low", "reason": "..."}}
  }},
  "overall_confidence": "high|med|low"
}}

DRAFT:
{draft}

Output JSON only."""


def _build_agent() -> Agent:
    return Agent(
        name="Verifier",
        model=OpenRouter(id=VERIFIER_MODEL, api_key=OPENROUTER_API_KEY),
        instructions=VERIFIER_SYSTEM,
        markdown=False,
    )


def verify(pdf_path: str, draft_json: Dict) -> Optional[Dict]:
    """Direct OpenRouter HTTP call (Agno's adapter doesn't pass type:file).
    Returns {corrections: {...}, overall_confidence: ...} or None on error."""
    import requests, time
    API_URL = "https://openrouter.ai/api/v1/chat/completions"
    pdf_b64 = base64.b64encode(Path(pdf_path).read_bytes()).decode()
    draft_str = json.dumps(draft_json, ensure_ascii=False)[:14000]
    payload = {
        "model": VERIFIER_MODEL,
        "messages": [
            {"role": "system", "content": VERIFIER_SYSTEM},
            {"role": "user", "content": [
                {"type": "file",
                 "file": {"filename": Path(pdf_path).name,
                          "file_data": f"data:application/pdf;base64,{pdf_b64}"}},
                {"type": "text", "text": VERIFIER_PROMPT_TMPL.format(draft=draft_str)},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 8000,
    }
    for attempt in range(3):
        try:
            t0 = time.time()
            r = requests.post(API_URL,
                              headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                                       "Content-Type": "application/json"},
                              json=payload, timeout=300)
            dt = time.time() - t0
            print(f"[Verifier] HTTP {r.status_code} {dt:.1f}s ({VERIFIER_MODEL})")
            if r.status_code == 200:
                j = r.json()
                if "choices" in j and j["choices"]:
                    return _parse_json(j["choices"][0]["message"]["content"])
            elif r.status_code == 429:
                time.sleep(2 ** (attempt + 1)); continue
            else:
                print(f"[Verifier] body: {r.text[:300]}")
        except Exception as e:
            print(f"[Verifier] exc: {e}")
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    return None


def apply_corrections(draft: Dict, verifier_out: Optional[Dict]) -> Dict:
    """Mutate draft in-place per verifier corrections.

    Path keys:
      'declaration.<field>'           → draft['declaration'][field] = value
      'items[<idx>].<field>'          → draft['items'][idx][field] = value
    """
    if not verifier_out or "corrections" not in verifier_out:
        return draft
    cor = verifier_out["corrections"] or {}
    decl = draft.setdefault("declaration", {})
    items = draft.setdefault("items", [])
    for path, fix in cor.items():
        if not isinstance(fix, dict):
            continue
        val = fix.get("value")
        if val is None:
            continue
        if path.startswith("declaration."):
            field = path.split(".", 1)[1]
            decl[field] = val
        else:
            m = re.match(r"items\[(\d+)\]\.(.+)", path)
            if m:
                idx, field = int(m.group(1)), m.group(2)
                while len(items) <= idx:
                    items.append({})
                items[idx][field] = val
    return draft


def _parse_json(raw: str) -> Optional[Dict]:
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    s = s.strip()
    if s.endswith("```"):
        s = s[:-3].strip()
    if "{" not in s:
        return None
    s = s[s.index("{"):s.rindex("}") + 1]
    for cand in [s,
                 re.sub(r"(\}\s*)\n(\s*\"[^\"]+\"\s*:)", r"\1,\n\2", s),
                 re.sub(r",(\s*[\}\]])", r"\1", s)]:
        try:
            return json.loads(cand)
        except Exception:
            continue
    return None
