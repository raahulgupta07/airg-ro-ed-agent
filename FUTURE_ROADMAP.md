# RO-ED AI Agent — Future Roadmap

> ⚠️ **SUPERSEDED (as of V11 Maestro).** Written against the pre-V11 "v2.0"
> architecture (5 hardcoded table agents, sync extraction). Current production is
> **V11 Maestro** — queue-driven (Redis + RQ), Postgres-backed, per-page classifier →
> parallel V7 (Veritas) + V10 PRO (Scrivener) → merge → reconcile. See README.md /
> CLAUDE.md for the real current state. The forward-looking ideas below remain
> aspirational, not started.

## Vision
Transform from a hardcoded Myanmar customs extraction tool into a **self-learning, multi-tenant document intelligence platform** that handles ANY document type, learns from every user correction, and improves automatically over time.

---

## Current State (v2.0)

```
PDF → Split → Vision LLM per page → Hardcoded Table Agents → Python Fixer → Results
```

- 12/12 test PDFs at 100% accuracy
- 5 table agents (Items, Declaration, Shipping, Licences, Insurance)
- 12 hardcoded Python fixer rules
- Completeness checker + Anomaly detection + Self-review
- Cost: ~$0.05-0.13/PDF
- Domain: Myanmar customs documents only

---

## Phase 1: Dynamic Agent Engine

**Goal:** Replace hardcoded prompts with config-driven prompt generation

### What Changes
- Store table definitions (fields, hints, rules) in DB instead of Python code
- One universal `execute_table_agent()` generates prompts from config
- Current hardcoded agents become "seed profile" (profile_id=1)
- Feature flag: `self_learning_enabled` (off by default, existing behavior preserved)

### New Files
```
v2/dynamic_executor.py    — universal agent executor
v2/rule_engine.py         — executes fixer rules from config
routes/profiles.py        — CRUD API for profiles
```

### New Tables
```
extraction_profiles       — stores table definitions, fixer rules, anomaly rules per doc type
prompt_cache              — generated prompt snapshots for debugging
```

### Result
Any user can define any extraction schema without code changes. Myanmar customs profile works identically to today.

---

## Phase 2: Schema Discovery

**Goal:** AI proposes what tables/fields to extract from any unknown document

### How It Works
```
Upload unknown PDF
  → Steps 1-3 run (universal page extraction)
  → No profile matches
  → SCHEMA DISCOVERY: single LLM call (~$0.01)
    "Here's what's on each page. Propose tables + fields."
  → AI returns: proposed tables, fields, types, hints, rules
  → User reviews in UI: approve / edit / add / remove
  → Saved as new profile
  → Extraction runs with approved schema
```

### New Files
```
v2/schema_discovery.py    — AI proposes schema from page data
v2/profile_matcher.py     — auto-matches documents to saved profiles
```

### New Table
```
profile_matches           — tracks which profile was used for which job
```

### Result
System handles ANY document type — medical reports, bank statements, legal contracts, construction permits — not just customs.

---

## Phase 3: Correction Feedback Loop

**Goal:** User corrections stored and used to improve extraction automatically

### How It Works
```
User sees extracted data → clicks a wrong value → types correct value → saves
  → Correction stored in DB
  → After 3 corrections on same field:
    → LEARNING ANALYSIS runs (background, free):
      → Detect pattern (numeric transform? prefix missing? wrong source?)
      → Generate new fixer rule automatically
      → Update extraction hints in prompt
      → Increment profile version
  → Next PDF uses improved profile
```

### New Files
```
v2/learning.py            — pattern detection + rule generation
routes/corrections.py     — correction submission API
```

### New Tables
```
corrections               — every user correction (original → corrected)
learning_events           — audit trail of all auto-generated rules
```

### Pattern Detection Categories
| Pattern | Example | Auto-Generated Rule |
|---------|---------|---------------------|
| Numeric transform | LLM returns 15 instead of 0.15 | `divide_by(100)` |
| Missing prefix | LLM drops "A-" from invoice number | `add_prefix_from_raw_text("A-")` |
| Wrong source | LLM grabs per-item value instead of total | `prefer_declaration_total` |
| Value mapping | "FREE" should be 0.0 | `map_value("FREE", 0)` |
| Regex cleanup | LLM includes "(**)" annotation | `regex_strip("\(\*+\)")` |

### Result
System gets better with every correction. Zero developer intervention needed.

---

## Phase 4: Confidence System

**Goal:** Per-field confidence scoring, flag low-confidence values for review

### Confidence Signals (all free, no LLM calls)
| Signal | Effect |
|--------|--------|
| LLM + page data agree on value | +0.1 |
| Self-review flags error | -0.3 |
| Python fixer changed value | set to 0.85 |
| Field corrected 3+ times historically | -0.2 |
| Value outside anomaly range | -0.2 |

### Display
- ≥ 0.8 → green (auto-accepted)
- 0.5-0.79 → amber (optional review)
- < 0.5 → red (mandatory review)

### New Files
```
v2/confidence.py          — multi-signal confidence computation
```

### New Table
```
field_confidence          — per-field confidence scores per job
```

### Result
Users only review what needs reviewing. High-confidence extractions flow through untouched.

---

## Phase 5: Frontend Enhancements

**Goal:** UI for corrections, confidence display, profile management, schema discovery

### New UI Features

#### Inline Editing (History page)
```
Click any extracted value → edit inline → save → triggers learning
```

#### Confidence Indicators
```
Each value has a colored dot: 🟢 green / 🟡 amber / 🔴 red
Red values highlighted, expandable to show "why low confidence"
```

#### Review Queue
```
New tab: shows all jobs with red/amber fields needing review
Filter by: field, profile, date, user
```

#### Profile Builder (/settings/profiles)
```
List of profiles → click to edit
Drag-drop field editor: add/remove/reorder fields per table
"Test on Sample PDF" button
Clone / Share profiles
Learning event log (what the AI learned and when)
```

#### Schema Discovery Modal
```
When unknown document uploaded:
  Show proposed tables/fields in editable form
  User approves → profile created → extraction runs
```

---

## Phase 6: Multi-Tenant

**Goal:** Each organization has own profiles, corrections, learned rules

### Changes
```
New table: tenants (id, name, plan, api_key)
Add tenant_id to: extraction_profiles, corrections, field_confidence, jobs
All queries filtered by tenant_id
Per-tenant cost tracking + billing
Admin dashboard per tenant
```

### Result
- Tenant A: Myanmar customs (food imports)
- Tenant B: Thailand customs (electronics)
- Tenant C: Medical lab reports
- Tenant D: Bank statements
- All isolated, all self-learning independently

---

## Phase 7: Profile Marketplace

**Goal:** Users share and clone profiles

### Features
```
"Myanmar Customs RO Import" → published as template
Other users can: browse → preview → clone → customize
Pre-built profiles for common document types
Community ratings + usage stats
```

---

## Phase 8: Advanced Intelligence

### Adaptive Model Selection
```
Simple pages (certificates, stamps) → cheap model (flash-lite)
Complex pages (multi-item tables, dense declarations) → smart model (gemini-2.5-pro)
Per-page model routing based on page_type + confidence history
```

### Cross-Document Intelligence
```
Same importer across shipments → track import history
Same B/L across declarations → link split shipments
Supplier relationship mapping
Price trend analysis per product
```

### Anomaly Learning
```
Anomaly ranges auto-adjust from actual data distribution
New anomaly types discovered from correction patterns
Per-tenant, per-profile anomaly baselines
```

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                     UNIVERSAL LAYER (any document)               │
│  Step 1: Split → Step 2: Vision LLM per page → Step 3: Page Map │
└────────────────────────────┬────────────────────────────────────┘
                             │ page_results (generic JSON)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PROFILE MATCHING                             │
│  Page-type fingerprint → Jaccard similarity → best profile       │
│  No match? → Schema Discovery (AI proposes, user approves)       │
└────────────────────────────┬────────────────────────────────────┘
                             │ matched profile
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     DYNAMIC AGENTS                               │
│  For each table in profile:                                      │
│    Generate prompt from config + correction examples             │
│    Call LLM → Parse → Validate                                   │
└────────────────────────────┬────────────────────────────────────┘
                             │ extracted data
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     RULE ENGINE                                  │
│  Apply fixer rules from profile (seed + learned)                 │
│  Compute per-field confidence (multi-signal)                     │
│  Run anomaly detection (profile-specific ranges)                 │
│  Run completeness check (profile-specific doc types)             │
└────────────────────────────┬────────────────────────────────────┘
                             │ results + confidence
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     USER REVIEW                                  │
│  High confidence → auto-accepted (green)                         │
│  Low confidence → flagged for review (red)                       │
│  User corrects → stored in corrections table                     │
└────────────────────────────┬────────────────────────────────────┘
                             │ corrections
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                     LEARNING ENGINE                              │
│  Pattern detection on corrections                                │
│  Auto-generate fixer rules                                       │
│  Update extraction hints in prompts                              │
│  Adjust anomaly ranges                                           │
│  Increment profile version                                       │
│  → Next PDF uses improved profile automatically                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Cost Projections

| Phase | Added Cost/PDF | Cumulative |
|-------|---------------|------------|
| Current (v2.0) | — | ~$0.13 |
| Phase 1: Dynamic Engine | $0 | ~$0.13 |
| Phase 2: Schema Discovery | +$0.01 (new types only) | ~$0.13 |
| Phase 3: Corrections | $0 | ~$0.13 |
| Phase 4: Confidence | $0 | ~$0.13 |
| Phase 5: Frontend | $0 | ~$0.13 |
| Phase 6: Multi-Tenant | $0 | ~$0.13 |
| Phase 8: Adaptive Models | -$0.02 (cheaper for simple pages) | ~$0.11 |

---

## Timeline Estimate

| Phase | Effort | Dependencies |
|-------|--------|-------------|
| Phase 1: Dynamic Engine | 3-4 days | None |
| Phase 2: Schema Discovery | 2-3 days | Phase 1 |
| Phase 3: Corrections Loop | 2-3 days | Phase 1 |
| Phase 4: Confidence | 1-2 days | Phase 3 |
| Phase 5: Frontend | 5-7 days | Phases 1-4 |
| Phase 6: Multi-Tenant | 3-5 days | Phase 5 |
| Phase 7: Marketplace | 3-5 days | Phase 6 |
| Phase 8: Advanced | Ongoing | All |

---

## Key Principle

> **The system should get smarter with every PDF it processes, without any developer writing a single line of code.**

The AI discovers. The user approves. The system learns. Repeat.

---

*Created by City AI Team — City Holdings Myanmar*
