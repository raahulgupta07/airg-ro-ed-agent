# Golden regression tests — exchange rate & math gates

These are the permanent guard against the exchange-rate extraction bug family
that Atlas V14 fixed: digital CUSDEC pages must yield a plausible, in-band FX
rate (never a stray customs figure like the old `500`/`636` artefacts, and
including 4-digit USD rates from the `\d{1,3}`→`\d{1,4}` regex fix); the
reconcile math gate must flag a rate the invoice×rate≈total math contradicts
while leaving a consistent rate alone; and the tax-completeness gate must flag a
customs total with no CORE tax (duty/CT/AT) present. Run them from `backend/`
with `python -m pytest tests/ -v`. The 4 math-gate unit tests (`reconcile`) need
no corpus and always run; the PDF-backed tests read 16 real CUSDEC PDFs that
live **outside the repo** — set `RO_ED_TEST_PDFS` to their directory (a
scratchpad fallback is baked in). When that directory or PyMuPDF is absent the
PDF-backed tests **skip** rather than fail, so CI on a machine without the corpus
stays green. The expected values (`golden_truth.json`) are version-controlled
here beside the tests; only the PDFs stay external.
