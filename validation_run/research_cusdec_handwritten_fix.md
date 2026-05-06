# Research Report: Fixing CUSDEC-1 Handwritten Extraction Failures in RO-ED

**Date:** 2026-05-05
**Scope:** Doc 8 (BALDUCCI / typed CUSDEC-1) + Doc 11 (Premium Topping / handwritten CUSDEC-1)
**Author:** Research pass for RO-ED-Lang pipeline
**Stack reviewed:** OpenRouter + Gemini 3 Flash (vision/assembler) + Claude Sonnet 4.6 (verifier) + 7-layer fee fallback

---

## 1. Executive Summary — Top 3 fixes that would solve ~80% of remaining issues

1. **Per-importer baseline cross-check (extend `importer_profiles.fee_baseline_json` → `extraction_baseline_json`).** The Premium Topping bug is the canonical "internally consistent but wrong" failure: currency/exch/invoice_price/unit_price all wrong but mutually arithmetic-consistent, so every sanity validator passes. The same importer + product was extracted correctly in 3 prior MACCS docs (D5/D6/D10) with currency=THB, exch≈57–60, unit≈60.88 THB/KG. A baseline lookup keyed on (importer_name, hs_code OR item_name_norm) catches this in one query. Cost: $0. ROI: very high — this single mechanism would have flagged Doc 11 immediately.

2. **Format-aware routing + handwriting-specific second pass with Gemini 2.5 Pro.** A cheap layout classifier (regex/keywords on page 1: "CUSDEC-1", "Import Declaration", absence of MACCS/QR-code, presence of handwritten ink density) routes old-format CUSDEC-1 docs to a stronger vision model. Gemini 3 Flash is great on printed/MACCS but documented to drop on handwriting; Gemini 2.5 Pro and Claude Sonnet 4.6 handle handwritten decimals (the "56·9266" → "5722.79" smudge) materially better. Cost: +~$0.05–0.08 per old-format PDF, only ~10–20% of corpus.

3. **Cross-field/cross-page corroboration with Chain-of-Verification (CoVe).** After assembler outputs, run an explicit CoVe step that asks: "Does the page text contain the literal token 'THB' or 'USD'? Does declaration currency match the commercial invoice currency on its own page? Does (item_unit × qty) ≈ declaration invoice_price (within 5%)?" Any disagreement → flag for low-confidence/manual review. CoVe reduced hallucinations 50–70% in the 2024 ACL paper and is cheap (<$0.01 per doc, text-only).

These three together address: (a) the wrong-but-consistent failure mode, (b) the handwritten misread, (c) the cascading error that propagates one bad field across 4 fields.

---

## 2. Findings by Research Question

### Q1. Myanmar Customs Document Standards

- **CUSDEC-1** is the legacy paper Import Declaration Form (still used; CUSDEC-2 export, CUSDEC-4 valuation). It is the form template behind Doc 8 and Doc 11.
- **MACCS** (Myanmar Automated Cargo Clearance System) is the digital successor. Initial rollout at Yangon Port ~2016, expansion ongoing through 2023–2025; full nationwide transition is incomplete — paper CUSDEC-1 still circulates for inland and smaller customs offices, which explains why old-format and MACCS docs coexist in the corpus.
- **Declaration number patterns observed:**
  - `100278958361` — modern MACCS (12-digit numeric)
  - `MA0241014920` — MACCS variant (2-letter prefix + 10 digits)
  - `MD-010642` — old CUSDEC-1 (2-letter prefix + dash + 6 digits)
  Recommendation: regex tier `^[A-Z]{2}[-]?\d{6,12}$` plus `^\d{10,14}$` covers all three. Reject anything that doesn't match either bucket and re-prompt.
- **Public databases:** Myanmar National Trade Portal (`myanmartradeportal.gov.mm`) hosts the HS tariff schedule (importable as a static lookup). No public per-declaration lookup, no public importer registry API.
- **Invoice numbering:** No standardized convention. Suppliers use their own. The Doc 8 bug ("MTGBIL12324000256" → "TGBIL12324000") is a regex-based stripping of the prefix `M` (mistaken as a stray letter) and trailing `256` (mistaken as page suffix). Recommendation: never strip the raw page-text invoice token; preserve it verbatim and flag if it doesn't match the commercial-invoice page.

### Q2. Vision LLM Techniques for Handwritten Docs

- **State of the art 2024–2025:** general-purpose VLMs (Gemini 2.5 Pro, GPT-4o, Claude 3.5/4 Sonnet) match or exceed traditional OCR on charts/handwriting per the OmniAI OCR benchmark, but accuracy drops to 80–90% on clear handwriting and far lower on poor scans. Decimal-point smudges are a known failure class.
- **Specialized models:**
  - **Donut** (OCR-free, encoder-decoder, ~1920p input) — strong on form understanding but you must fine-tune; not worth it for a 13-doc corpus.
  - **LayoutLMv3** — needs OCR boxes + image; SOTA on FUNSD/CORD; better for fixed-template forms than mixed CUSDEC-1/MACCS.
  - **Florence-2** — small VLM, weak out-of-the-box per practitioner reports.
  - **PaddleOCR (PP-StructureV3 / VI-LayoutXLM)** — strongest open-source OCR for invoices in 2025, beats EasyOCR/Tesseract on multi-column tables by ~12%, supports 100+ languages including Myanmar script.
- **Ensemble (OCR + LLM):** the 99%-accurate-invoice case study (Towards AI) explicitly used PaddleOCR text + LLM reasoning. The PaddleOCR text gives a literal character stream that catches things the VLM "imagines" — exactly the Premium Topping decimal-point bug.
- **Pre-processing:** for CamScanner-style PDFs (300 DPI, already enhanced), the highest-yield steps are: (1) adaptive thresholding only on suspect regions (currency/numeric fields), (2) deskew (Hough lines), (3) contrast-limited adaptive histogram equalization (CLAHE) — but only on cropped field regions, not the whole page. Whole-page binarization usually hurts VLMs since they were trained on color/grayscale.
- **Decimal-point detection:** there is no pure ML fix. The reliable fix is structural: run a **dedicated numeric-field re-prompt** ("crop to currency-code box / exchange-rate box and read only the digits and decimal mark") at higher temperature=0 with a strict regex schema `^\d{1,3}(\.\d{1,4})?$`. Cross-check against PaddleOCR text on the same crop.

### Q3. Self-Consistency Detection Strategies

- **Wang et al. 2022 self-consistency** (arxiv 2203.11171): sample N reasoning paths at temp>0, majority-vote. Gains +12–18% on arithmetic. For RO-ED: run vision on suspect numeric fields N=3 with temp=0.4, take mode. Cost: 3× vision step on suspect fields only (~$0.006/doc).
- **CoVe (Dhuliawala et al. 2024 ACL)** plans verification questions, answers them in isolation, then revises. 50–70% hallucination reduction. Direct application to RO-ED: after assembler emits the 18 declaration fields, generate 5–8 verification questions ("Does the page contain literal 'THB'?", "Does qty × unit ≈ total?") and execute against page text only.
- **Cross-field arithmetic**: the Premium Topping case has `qty=21,840 KG × 60.88 THB/KG = 1,329,619 THB ≈ stated invoice_price`. With wrong currency the arithmetic shows `21,840 × 0.6056 = 13,226 USD` — also internally consistent. But `21,840 × 60.88 ÷ 5722.79` doesn't equal 13,226 — it equals 232. **The exchange-rate cross-check is what breaks the wrong-but-consistent loop**: customs_value_mmk / invoice_price should equal exchange_rate within 1%. If the four fields don't algebraically close, at least one is wrong.
- **Magnitude/plausibility check**: "$0.6/KG for whip topping cream from Thailand" is implausibly cheap (real prices ~$2–3/KG FOB). A simple HS-code → typical-unit-price-USD-range lookup table flags this.

### Q4. Document Format Detection

- **Cheapest effective heuristic:** keyword + layout signature on page 1.
  - MACCS marker: presence of QR code + "MACCS" string + 12+ digit decl number.
  - CUSDEC-1 marker: phrase "Customs Department Import Declaration" or "CUSDEC-1" + 2-letter+digits decl pattern.
  - Handwriting marker: ink-density variance in numeric fields (compute per-cell pixel-stroke entropy on cropped fields).
- **LLM classifier** (Gemini 3 Flash with single-prompt "is this MACCS or CUSDEC-1, and is the body handwritten?") is 1 call, ~$0.001, near-perfect on this corpus. Worth doing as Step 1.5 between splitter and vision.
- **Mixed Myanmar/English** is already handled by Gemini; PaddleOCR has a `--lang my` model if added.

### Q5. Importer Baseline / Learning

- The existing `importer_profiles.fee_baseline_json` is the right place to extend. Add:
  - `currency_history`: list of last N=20 currencies seen, with counts.
  - `exchange_rate_p10_p90`: per (importer, currency) sliding window — flag if new value outside [p10*0.7, p90*1.3].
  - `unit_price_by_hs`: per (importer, hs_code) median + IQR; flag outside median ± 3·IQR.
- **3-sigma vs IQR:** for trade data, IQR is more robust (long-tail prices). Use 1.5×IQR for warn, 3×IQR for hard-flag.
- **Bootstrap problem**: first-time importers have no baseline. Fall back to (hs_code, country) global baseline aggregated across all importers.
- **Drift risk:** legitimate currency shift (importer switches THB → USD supplier) would be flagged. Mitigation: require 2 consecutive flagged extractions before auto-rejecting; first occurrence routes to manual review queue (the existing `<80%` confidence escalate flow).

### Q6. Multi-Step Verification Architectures

- **Anthropic verifiable LLMs / Constitutional AI:** post-hoc critique-and-revise patterns. The current verifier (Sonnet against page images) already does this for layout but not for cross-field arithmetic.
- **CoVe** as above — most directly applicable.
- **LLM-as-Judge frameworks (RAGAS, DeepEval):** designed for QA/RAG eval; for IE the more direct analog is field-level "groundedness": for every emitted field, retrieve the supporting text span; if no span supports the value, mark not-grounded. Implementable cheaply: ask Gemini 3 Flash "for each field, return the literal page-text span you used."
- **Hybrid OCR + LLM ensemble:** PaddleOCR text stream → LLM reads stream → if LLM-from-image and LLM-from-OCR-text disagree, human review.

### Q7. Currency / Exchange Rate Bug — Specific Techniques

- **Page-text token presence:** if assembler says currency=USD, search page text for "USD"; if literal "USD" string is absent and "THB" is present, flip. (Doc 11 page text contains "THB" handwritten in Currency Code field; the "USD" stamp is a printed box label, not a value.)
- **Exchange-rate sanity by date:** April 2024 MMK→USD was ~3,100; MMK→THB was ~85. A claimed exchange rate of 5722.79 for *any* currency in April 2024 is impossible. Hard rule: `exchange_rate < 200` → cannot be USD; `exchange_rate < 100` → likely THB or SGD. Implementation: hard-coded reference bands (exch is slow-moving and these never overlap).
- **CBM API:** `forex.cbm.gov.mm/api/history/{dd-mm-yyyy}` provides USD/CHF/BDT/SGD/JPY/GBP/AUD/EUR/INR/PHP. **THB is not in CBM's published basket** — workaround: use a free FX API (exchangerate.host, Frankfurter) for THB→MMK historical, or compute via THB→USD (yfinance) × CBM USD→MMK.
- **Cross-page corroboration:** declaration page currency vs commercial-invoice page currency. If they disagree, surface both and prefer the commercial invoice (it's the supplier's source-of-truth document).

### Q8. External Data Cross-Reference

- **CBM exchange-rate sanity** — high ROI, low effort. One API call per doc, cache by date. Cost: $0.
- **HS code lookup** — Myanmar tariff schedule downloadable. Use as: validate hs_code matches a real entry; validate item_name fuzzy-matches HS description.
- **Importer registration** — no public API; skip.

### Q9. Failure Mode Analysis Framework

Five categories observed and recommended fix routing:

| Category | Example | Fix |
|---|---|---|
| Vision miss (field absent in output) | already handled | existing QA re-run |
| Vision wrong-but-confident | Doc 11 currency/exch | Q3 cross-arithmetic + Q5 baseline |
| Label/value confusion | Doc 8 invoice no = Import License no | Q4 format-aware prompts + Q6 grounded-span check |
| Format mismatch | CUSDEC-1 prompts written for MACCS | Q4 routing |
| Handwriting unreadable | smudged decimal | Q2 numeric-field re-prompt + PaddleOCR cross-check |

### Q10. Practical Recommendations for Current Stack

- **Add Gemini 2.5 Pro as conditional second pass** for CUSDEC-1 + handwritten only. Different model family from Gemini 3 Flash → different error modes → disagreement = signal. ~$0.05/doc, only ~20% of corpus → ~$0.01 amortized.
- **Add PaddleOCR (PP-StructureV3)** as parallel raw-text extractor. Free, runs locally. Use for: (a) literal token search ("THB" / "USD" presence), (b) numeric field cross-check, (c) Myanmar-script field reading.
- **Importer-specific extraction templates:** not needed; per-importer baselines (Q5) achieve the same goal with less maintenance.
- **Confidence-reject + manual review flow** is already in the codebase (`<80%` escalate). Hook the new arithmetic-closure and baseline checks into this same flow — failure = drop confidence to 0.6 = auto-escalate.

---

## 3. Concrete Implementation Roadmap (ranked by ROI)

| # | Change | Effort | Cost / PDF | Catches |
|---|---|---|---|---|
| 1 | **Cross-field arithmetic closure check** in `step4_validate.py`: `qty × unit_price ≈ invoice_price`, `invoice_price × exch_rate ≈ customs_value_mmk` (5% tolerance). On fail, drop confidence + audit. | 1 day | $0 | Doc 11, future cascading errors |
| 2 | **Per-importer + per-(importer,HS) baseline** extension to `importer_profiles`. Compute median/IQR after each verified extraction; on new extraction, flag outside IQR×3. | 2 days | $0 | Doc 11 (THB seen 3 times prior) |
| 3 | **Hard exchange-rate band check**: `exch < 100 → not USD`, `exch < 200 → not USD/EUR`, `exch > 200 with currency=USD/EUR → ok`. Per-currency Apr-2024 plausibility table. | 0.5 day | $0 | Doc 11 (5722.79 is impossible for any currency) |
| 4 | **Page-text literal token search**: regex "THB"/"USD"/"EUR" in raw vision text per page; if currency value not present in any page → flag. | 0.5 day | $0 | Doc 11 |
| 5 | **Format classifier** (LLM single call) routing CUSDEC-1 vs MACCS; CUSDEC-1 + handwritten → Gemini 2.5 Pro pass. | 2 days | +$0.001 classifier, +$0.05 on 20% docs = $0.011 amortized | Doc 8 + Doc 11 |
| 6 | **CoVe verification step** (text-only): generate 5–8 verification Qs per doc, answer in isolation, revise. | 3 days | +$0.005 | All hallucination-class errors |
| 7 | **PaddleOCR parallel text extractor** + disagreement flag with VLM. | 4 days (Docker integration, +PyTorch dep) | $0 (local) | Doc 8 (invoice-no preservation), Doc 11 (decimal smudge) |
| 8 | **Self-consistency N=3 sampling** on suspect numeric fields only (currency/exch/unit_price). | 2 days | +$0.006 | Marginal cases on top of #1–#5 |
| 9 | **CBM exchange-rate API cross-check** (USD only — THB not supported by CBM, use Frankfurter for THB). Cache by date. | 1 day | $0 | Wrong-rate detection |
| 10 | **Field-level groundedness span**: assembler returns the page-text span supporting each value. Empty span → flag. | 2 days | +$0.002 | Doc 8 (no real "invoice number" span supports `MTGBIL...`) |

**Recommended implementation order:** 1, 3, 4, 2 (immediate, $0 cost, catches both observed bugs). Then 5, 6 (medium effort, modest cost). Then 7, 8, 9, 10 (incremental gains).

---

## 4. Cost Analysis

Current pipeline: ~$0.15–0.20 / PDF.

| Addition | Marginal cost / PDF | When charged |
|---|---|---|
| #1 arithmetic closure | $0 | always |
| #2 baseline | $0 | always |
| #3 exch band | $0 | always |
| #4 token search | $0 | always |
| #5 format classifier | $0.001 (always) + $0.05 (CUSDEC-1 only, ~20%) | conditional |
| #6 CoVe | $0.005 | always |
| #7 PaddleOCR | $0 (local, +CPU time ~3s/page) | always |
| #8 self-consistency N=3 | $0.006 | only on flagged suspect fields |
| #9 CBM API | $0 | always (cached by date) |
| #10 grounding span | $0.002 | always |

**Worst-case all-on cost:** ~$0.20 + $0.025 = **~$0.225 / PDF** (12.5% increase).
**Realistic (#1–#6 only):** ~$0.20 + $0.017 = **~$0.217 / PDF**.
**Minimum-fix bundle (#1–#4):** **$0 marginal**, catches both observed bugs.

---

## 5. References / Sources

**Papers / Methods**
- Wang et al., *Self-Consistency Improves Chain of Thought Reasoning in Language Models*, 2022 — https://arxiv.org/abs/2203.11171
- Dhuliawala et al., *Chain-of-Verification Reduces Hallucination in Large Language Models*, ACL 2024 Findings — https://aclanthology.org/2024.findings-acl.212/
- Survey on MLLM-based Visually Rich Document Understanding (2025) — https://arxiv.org/html/2507.09861
- LENS: Learning Ensemble Confidence from Neural States (2025) — https://arxiv.org/html/2507.23167v1

**Models / Libraries**
- PaddleOCR (PP-StructureV3, 100+ languages, VI-LayoutXLM) — https://github.com/PaddlePaddle/PaddleOCR
- Donut (OCR-free document understanding) — referenced in Unstructured.io VLM intro
- LayoutLMv3 — KUNGFU.AI engineering explainer; Nanonets LayoutLM guide
- Florence-2 — Nanonets VLM-for-data-extraction guide

**Benchmarks**
- OmniAI OCR Benchmark (VLM vs traditional OCR) — https://getomni.ai/blog/ocr-benchmark
- Top 6 OCR Models 2025 (MarkTechPost) — https://www.marktechpost.com/2025/11/02/comparing-the-top-6-ocr-optical-character-recognition-models-systems-in-2025/

**Industry / Practitioner**
- "How We Built a 99% Accurate Invoice Processing System Using OCR and LLMs" (Towards AI) — PaddleOCR + LLM ensemble case study
- Document Intelligence with LLMs guide (Virtido, 2026)

**Myanmar Customs**
- Myanmar National Trade Portal — Guide to Importing Goods — https://www.myanmartradeportal.gov.mm/en/guide-to-import
- MACCS overview — https://myanmar.gov.mm/-/myanmar-automated-cargo-clearance-system-maccs-
- Central Bank of Myanmar Forex API — https://forex.cbm.gov.mm/index.php/api (note: THB not in published basket; fall back to Frankfurter / exchangerate.host)
- Myanmar Customs Department — https://customs.gov.mm/

**Preprocessing**
- Survey on Image Preprocessing Techniques to Improve OCR Accuracy (Medium / Technovators)
- Docparser: Improve OCR Accuracy with Advanced Image Preprocessing
