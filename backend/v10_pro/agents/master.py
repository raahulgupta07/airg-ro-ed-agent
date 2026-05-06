"""V9 — Master orchestrator agent (Opus).
Has access to all PDF/math/db tools. Decides which tools to call when the
draft extraction has uncertainties or arithmetic mismatches.

NOTE: This agent is OPTIONAL — for MVP the workflow.py runs filter→reader→verifier→postfix
sequentially without needing the master to make tool decisions. Master is
exposed here for future use (e.g. focused box-zoom on uncertain decl no).
"""
from agno.agent import Agent
from agno.models.openrouter import OpenRouter

from v10_pro.config import MASTER_MODEL, OPENROUTER_API_KEY
from v10_pro.tools.pdf_tools import render_page, extract_text, slice_pdf, box_zoom
from v10_pro.tools.math_tools import arithmetic_audit, currency_band_check, deterministic_postfix
from v10_pro.tools.db_tools import consignor_canonical, hs_lookup, origin_history


MASTER_SYSTEM = """You orchestrate Myanmar customs PDF extraction.
You have tools for rendering pages, reading text, slicing PDFs, zooming on
suspect boxes, validating arithmetic, checking currency bands, and looking up
canonical consignor names / HS codes / origin history.

Strategy:
1. Use extract_text to find declaration pages (look for 'Declaration No', etc).
2. Use render_page or slice_pdf to focus the work.
3. After receiving a draft JSON, use arithmetic_audit and currency_band_check
   to flag suspect fields.
4. For each suspect, use box_zoom at 400 DPI to re-read at high precision.
5. Use consignor_canonical / hs_lookup to normalize names and codes.
6. Return final JSON matching the v9.schemas.ExtractionResult shape."""


def build_master() -> Agent:
    return Agent(
        name="CustomsMaster",
        model=OpenRouter(id=MASTER_MODEL, api_key=OPENROUTER_API_KEY),
        tools=[render_page, extract_text, slice_pdf, box_zoom,
               arithmetic_audit, currency_band_check, deterministic_postfix,
               consignor_canonical, hs_lookup, origin_history],
        instructions=MASTER_SYSTEM,
        markdown=False,
    )
