# FLOW.md — how a PDF becomes an approved declaration

State of the tree on 2 Aug 2026: **ATLAS V14 is the only engine.** ROVER PRO,
ROSETTA, `presto` and `classic` are retired; a request naming one is redirected to
ATLAS and the substitution is emitted, never done quietly.

---

## 1. Request → worker → browser

```mermaid
flowchart TD
  U["Browser · /agent<br/>drop PDF, EXECUTE"] -->|"POST /api/extract-v11<br/>bearer JWT"| API["FastAPI app<br/>2 uvicorn workers"]
  API -->|"202 + stream_id"| U
  API --> OWN["Redis v11:owner:{stream_id}<br/>binds job to its creator"]
  API --> Q["RQ enqueue · Redis"]
  Q --> W["worker container<br/>WORKER_REPLICAS=3"]
  W --> WF["v11/workflow.run(pdf, job_id, engine)"]
  WF -->|"_bus_emit"| PS["Redis pubsub<br/>v11:events:{job_id}<br/>+ history list"]
  U -->|"EventSource ?token=jwt"| SSE["GET /api/extract-v11/stream/{id}"]
  PS --> SSE --> U
  WF --> PG[("Postgres<br/>jobs · declarations · items")]
  PG --> REV["/review · /checks · /history<br/>human approve"]
```

SSE replays history from Redis, so a page refresh reopens the stream and the whole
log comes back.

## 2. Inside ATLAS V14

```mermaid
flowchart TD
  A["engine intercept<br/>anything != atlas → ATLAS, warn"] --> B["Phase 1 · PageClassifier<br/>PRINTED / INKED / EXTRA"]
  B --> C["Phase 1.5 · triage<br/>doc_class · cusdec_page · cusdec_page_digital<br/>single authority"]
  C --> D["Phase 2 · split by label"]
  D --> E{"typed pages digital?"}
  E -->|yes| P["V14-1 Swift · Presto<br/>text layer → 1 schema call"]
  E -->|no| V7["Atlas Classic · V7 Veritas<br/>page images"]
  P -->|"gate fails"| SC["self_correct<br/>re-read broken header field"]
  SC -->|"still fails"| V7
  D --> HW["V14-2 Vision · Scribe<br/>hi-DPI vision vote"]
  P --> M
  V7 --> M
  HW --> M["Phase 4 · merge"]
  V7 -.->|"CUSDEC is a scan →<br/>drop typed-lane header"| S39["Phase 3.9 scope guard"]
  S39 --> M
  M --> TL["textlayer_header<br/>coordinates beat the model<br/>sole source of the 3 lifecycle dates"]
  TL --> R1["4.25 item-count recover<br/>4.3 empty-decl rescue"]
  R1 --> R2["4.35 cusdec_rescue · text<br/>4.36 vision rescue · scanned, retry once"]
  R2 --> R3["4.365 item sum corrects stamped total ≤1%<br/>4.37 derive adjustment from CIF<br/>4.38 neighbour-value guard"]
  R3 --> G["Phase 4.4 · reconcile gate"]
  G --> BB["4.5 field_bboxes<br/>scoped to declaration pages"]
  BB --> DB["Phase 5 · _save_to_db"]
  DB --> DONE["DONE event"]
```

## 3. The gate — `v11/tools/reconcile.py`

```mermaid
flowchart TD
  IN["declaration + items"] --> AN{"declared total?"}
  AN -->|no| DUTY["fallback anchor<br/>customs_duty / duty_rate"]
  DUTY -->|"still none"| NC["checked=false · balanced=false<br/>never fake a pass"]
  AN -->|yes| K1{"|total − Σ items| ≤ 5%"}
  K1 --> K2{"CIF closure<br/>(inv+frt+ins+adj)×rate ≈ total"}
  K2 -->|"fails but declared > computed<br/>and items exact"| UP["cif_uplift_only<br/>assessment above invoice is normal"]
  K2 --> K3{"a CORE tax present?<br/>CD / CT / AT"}
  K3 --> K4{"exchange rate survives<br/>currency band + math cross-check"}
  K1 & K2 & K3 & K4 --> BAL["balanced = all four"]
  UP --> BAL
  BAL -->|true| OK["cross_val_passed=1"]
  BAL -->|false| FIX{"item sum short<br/>AND attachment pages exist?"}
  FIX -->|yes| REC["re-extract attachment slice<br/>keep only if gap shrinks"]
  REC --> BAL
  FIX -->|no| FLAG["needs_review=true"]
  OK --> J["JUDGE + learn priors<br/>advisory, can only ADD review"]
  FLAG --> J
```

Advisory, never flips `balanced`: duty closure (exemptions legitimately break it)
and the per-row check (`value ≈ qty × price × rate` → `bad_rows`), which does force
review.

## 4. Where a header date comes from

The only diagram here that traces provenance rather than sequence. Since
4 Aug 2026 the three lifecycle dates — `arrival_date`, `release_order_date`,
`completion_date` — are read off the page and never asked of a model.

```mermaid
flowchart TD
  D["arrival_date · release_order_date · completion_date"] --> T{"page has a text layer?"}
  T -->|yes| TH["textlayer_header<br/>coordinate-anchored · $0"]
  TH -->|"label not found"| FR["formread labelled-date spec<br/>decoy exclusion · $0"]
  T -->|no| N["null"]
  FR -->|"still nothing"| N
  N --> ISS["issues.py surfaces the blank<br/>to the reviewer"]
  TH --> DB[("declarations")]
  FR --> DB
  N --> DB
  SC["scanned bundle only:<br/>vision_rescue arrival_date"] -.->|"overrules a waybill<br/>reading from the typed lane"| DB
```

Blank is not broken. A scanned declaration has nothing for a reader to read,
and the model used to fill the empty row by echoing a neighbouring date — two
documents were found carrying an arrival date and a release-order date printed
nowhere in the file, both equal to that document's declaration date
(`rover/supervisor.flag_echoed_dates`). **A date has no arithmetic to fail, so
no gate catches it.** A reviewer can see a blank; they cannot see an echo.

`arrival_date` in `vision_rescue` is the deliberate exception: it is the only
thing that can overrule an arrival date the typed lane scraped off an
attachment, which is why `workflow.py` treats that source as authoritative.

The columns, the review screen, the `/declarations` date filter and the Excel
field map are unchanged — the team's ledger keys on the Release-Order date.

## 5. Human loop

```mermaid
flowchart LR
  P["pending_review"] --> RV["ReviewSplitView<br/>PDF beside the form"]
  CH["/checks<br/>per-field evidence queue"] --> RV
  RV -->|"cell edit"| FE["field_edits audit row"]
  FE --> ACC["bump_field_correction<br/>field_accuracy"]
  RV --> AP["approve"] --> GOLD["golden corpus · priors<br/>all flag-gated, advisory"]
  RV --> RJ["reject"]
  RV --> RR["rerun → parent_job_id"]
  AP --> XL["Excel export<br/>Product Items 13 col · Declaration 23 col"]
```

Learning is subordinate to the arithmetic: JUDGE hard-interlock means a learned
hint can never override reconcile.
